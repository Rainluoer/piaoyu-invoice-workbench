"""本地发票工作台。仅监听回环地址，原票保留，重复副本可回收。"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import socket
import threading
import time
import webbrowser
import zipfile
from folder_picker import pick_folder
from datetime import datetime
from contextlib import closing
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from receipt_jobs import ReceiptJobs
from invoice_company import seller_from_text, company_from_pdf, company_needs_repair, PDFIUM_LOCK
from pdf_layout import layout_text
from invoice_extract import from_pages
from invoice_category import classify_invoice
from pdf_layout import layout_lines

BASE = Path(__file__).resolve().parent
EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg'}
MAX_FILE = 40 * 1024 * 1024
MAX_ZIP = 200 * 1024 * 1024
STATUSES = {'待报销', '报销中', '已报销'}
CATEGORIES = ('其他票据', '出行交通', '科研材料', '铁路客票', '住宿费')
IGNORED_DIRECTORIES = ()


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def money(value):
    if value in ('', None):
        return None
    try:
        amount = Decimal(str(value).replace(',', ''))
        if not amount.is_finite() or abs(amount) > 100000000:
            raise ValueError('金额超出范围')
        return int(amount.quantize(Decimal('.01')) * 100)
    except InvalidOperation:
        raise ValueError('金额格式不正确')


def safe_name(name):
    # ZIP 文件名可能采用 GBK 而未设置 UTF-8 标记。
    name = name.replace('\\', '/').split('/')[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:160]
    if not name or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        name = '票据_' + secrets.token_hex(4) + Path(name).suffix
    return name


def parse_text(text, filename):
    flat = re.sub(r'[ \t\u3000]', '', text)
    match = re.search(r'(?:发票号码|发票号|票据号码)[:：]?(\d{8,30})', flat)
    number = match.group(1) if match else ''
    if not number:
        # 某些电子发票文字顺序将标签与内容分开。只接受唯一的 20 位候选。
        candidates = set(re.findall(r'(?<!\d)\d{20}(?!\d)', flat))
        if len(candidates) == 1:
            number = candidates.pop()
    date_match = re.search(r'开票日期[:：]?(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})', flat)
    if not date_match:
        candidates = list(re.finditer(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})', flat))
        if len({m.group(0) for m in candidates}) == 1:
            date_match = candidates[0]
    date = ''
    if date_match:
        try:
            date = datetime(*(int(v) for v in date_match.groups())).strftime('%Y-%m-%d')
        except ValueError:
            pass
    amount_match = re.search(r'小写[)）]?[:：]?[¥￥]?(-?[\d,]+\.\d{2})', flat)
    if not amount_match and ('铁路' in text or '客票' in text):
        amount_match = re.search(r'[¥￥](-?[\d,]+\.\d{2})', flat)
    cents = money(amount_match.group(1)) if amount_match else None
    items = re.findall(r'^\s*\*([^*\n]+)\*([^\n]+)', text, re.M)
    title = (items[0][0] + ' · ' + re.split(r'\s{2,}|\s+\d', items[0][1])[0].strip()) if items else Path(filename).stem
    if '铁路' in text or '铁路' in filename:
        title = '铁路电子客票'
    seller = seller_from_text(text)
    return dict(invoice_no=number, date=date, amount_cents=cents, title=title[:160], seller=seller)


def is_itinerary(row):
    """识别作为附件的行程单，不因发票备注提及行程单而隐藏真正发票。"""
    lines = [re.sub(r'\s+', '', line) for line in (row.get('text') or '').splitlines() if line.strip()]
    if any(re.match(r'^(?:电子发票|增值税.{0,24}发票)', line) for line in lines[:8]):
        return False
    names = ' '.join(row.get(field) or '' for field in ('filename', 'title'))
    if re.search(r'行\s*程\s*单|\bitinerary\b', names, re.I):
        return True
    return any((('行程单' in line and len(line) < 80 and not line.startswith(('备注', '说明', '温馨提示')))
                or line.upper() in ('ITINERARY', 'AMAPITINERARY')) for line in lines[:3])


class Store:
    def __init__(self, root, data_dir):
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / 'imports').mkdir(exist_ok=True)
        self.db_path = self.data_dir / 'invoices.sqlite3'
        self.lock = threading.RLock()
        self.key = secrets.token_urlsafe(32)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, filename TEXT NOT NULL,
                sha256 TEXT NOT NULL, mtime_ns INTEGER, size INTEGER, title TEXT,
                invoice_no TEXT, date TEXT, amount_cents INTEGER, category TEXT,
                seller TEXT DEFAULT '', status TEXT, project TEXT DEFAULT '', notes TEXT DEFAULT '',
                text TEXT DEFAULT '', warning TEXT DEFAULT '', source TEXT DEFAULT '',
                manual INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT, action TEXT,
                detail TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.db.commit()
        saved_root = self.db.execute("SELECT value FROM meta WHERE key='invoice_root'").fetchone()
        saved_roots = self.db.execute("SELECT value FROM meta WHERE key='invoice_roots'").fetchone()
        if saved_root:
            self.root = Path(saved_root[0]).resolve()
        self.managed_roots = [self.root]
        if saved_roots:
            self.managed_roots = list(dict.fromkeys([self.root, *[Path(value).resolve() for value in json.loads(saved_roots[0])]]))
        if 'page_count' not in {row[1] for row in self.db.execute('PRAGMA table_info(invoices)')}:
            self.db.execute('ALTER TABLE invoices ADD COLUMN page_count INTEGER DEFAULT 1')
            self.db.commit()
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(invoices)')}
        for column in ('deleted_at', 'trash_path', 'purged_at'):
            if column not in columns:
                self.db.execute(f"ALTER TABLE invoices ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.db.commit()
        version = self.db.execute("SELECT value FROM meta WHERE key='parser_version'").fetchone()
        self.parser_changed = not version or version[0] != '3'
        company_version = self.db.execute("SELECT value FROM meta WHERE key='company_parser_version'").fetchone()
        self.company_parser_changed = not company_version or company_version[0] != '2'
        self.receipts = ReceiptJobs(self)

    def event(self, id_, action, detail):
        self.db.execute('INSERT INTO events(invoice_id,action,detail,created_at) VALUES(?,?,?,?)', (id_, action, detail, now()))

    @staticmethod
    def ignored_path(value):
        path = Path(value).resolve()
        return any(path == directory or path.is_relative_to(directory) for directory in IGNORED_DIRECTORIES)

    def source_files(self, root):
        """扫描时剪掉被忽略的目录，避免读取其票据、ZIP、文档及元数据。"""
        root = Path(root).resolve()
        if self.ignored_path(root):
            return
        for current, directories, filenames in os.walk(root, followlinks=False):
            current = Path(current)
            kept = []
            for name in directories:
                directory = current / name
                resolved = directory.resolve()
                if (not directory.is_symlink() and resolved.is_relative_to(root)
                        and not self.ignored_path(resolved) and resolved != BASE
                        and not resolved.is_relative_to(BASE) and resolved != self.data_dir
                        and not resolved.is_relative_to(self.data_dir)):
                    kept.append(name)
            directories[:] = kept
            for name in filenames:
                yield current / name

    def ignored_invoice(self, row):
        if self.ignored_path(row['path']):
            return True
        # 旧 ZIP 解压副本位于 imports；仍按原 ZIP 来源应用目录排除。
        source = row.get('source') or ''
        return (Path(row['path']).resolve().is_relative_to(self.data_dir / 'imports')
                and not source.startswith('手动导入') and bool(source)
                and any(self.ignored_path(root / source) for root in self.managed_roots))

    def set_invoice_root(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise ValueError('请填写发票文件夹的完整路径')
        candidate = Path(value.strip().strip('"')).expanduser()
        if not candidate.is_absolute():
            raise ValueError('请使用完整的绝对路径')
        try:
            candidate = candidate.resolve(strict=True)
            if not candidate.is_dir():
                raise ValueError('请选择文件夹，不能选择文件')
            if candidate.is_relative_to(BASE) or candidate.is_relative_to(self.data_dir):
                raise ValueError('不能将工作台程序或数据目录设为发票目录')
            with os.scandir(candidate):
                pass
        except OSError:
            raise ValueError('文件夹不存在或无法读取，请检查路径和权限') from None
        if not self.receipts.active.acquire(blocking=False):
            raise ValueError('入库单正在生成，请等待完成后切换目录')
        try:
            with self.lock:
                self.root = candidate
                self.managed_roots = list(dict.fromkeys([*self.managed_roots, candidate]))
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('invoice_root', str(candidate)))
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('invoice_roots', json.dumps([str(path) for path in self.managed_roots], ensure_ascii=False)))
                    self.event(None, '切换发票目录', str(candidate))
                return dict(root=str(candidate), scan=self.scan())
        finally:
            self.receipts.active.release()

    def category(self, source, text='', title='', filename=''):
        return classify_invoice(source, text, title, filename)

    def metadata(self):
        result = {}
        for path in self.source_files(self.root):
            if path.name != 'invoice_data.json':
                continue
            if self.data_dir.parent in path.parents or self.ignored_path(path):
                continue
            try:
                records = json.loads(path.read_text(encoding='utf-8-sig'))
                for record in records if isinstance(records, list) else []:
                    if isinstance(record, dict) and record.get('invoice_no'):
                        result[str(record['invoice_no'])] = record
            except (ValueError, OSError, TypeError):
                continue
        return result

    def ingest(self, path, source, metadata):
        path = path.resolve()
        if self.ignored_path(path) or path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            return 'skipped'
        stat = path.stat()
        old = self.db.execute('SELECT * FROM invoices WHERE path=?', (str(path),)).fetchone()
        if old and old['deleted_at']:
            return 'skipped'
        if old and old['mtime_ns'] == stat.st_mtime_ns and old['size'] == stat.st_size and not self.parser_changed:
            if self.company_parser_changed and (not old['seller'] or company_needs_repair(old['seller'])):
                seller = seller_from_text(old['text'])
                if not seller and path.suffix.lower() == '.pdf':
                    seller = company_from_pdf(path)
                if seller:
                    self.db.execute('UPDATE invoices SET seller=?,updated_at=? WHERE id=?', (seller, now(), old['id']))
                    return 'updated'
            return 'unchanged'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not old:
            # 移动文件后沿用原台账记录，保留已填写的项目、状态和备注。
            previous = self.db.execute("SELECT * FROM invoices WHERE sha256=? AND deleted_at=''", (digest,)).fetchall()
            moved = next((row for row in previous if not Path(row['path']).exists()), None)
            if moved:
                self.db.execute('UPDATE invoices SET path=?,filename=?,source=?,mtime_ns=?,size=?,updated_at=? WHERE id=?', (str(path), path.name, source, stat.st_mtime_ns, stat.st_size, now(), moved['id']))
                self.event(moved['id'], '路径更新', source)
                return 'updated'
        text, warning, page_count = '', '', 1
        reconstructed = None
        if path.suffix.lower() == '.pdf':
            try:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    page_count = len(pdf.pages)
                    text = '\n'.join(p.extract_text() or '' for p in pdf.pages[:8])[:60000]
                    initial = parse_text(text, path.name)
                    # 基础字段齐全也可能把单位/规格混进品名，仍按列位置核对商品。
                    try:
                        reconstructed = from_pages([layout_lines(p) for p in pdf.pages[:8]])
                        if initial['amount_cents'] is None or not initial['invoice_no'] or not initial['date']:
                            text = '\n'.join(layout_text(p) for p in pdf.pages[:8])[:60000]
                    except ValueError:
                        pass
                if not text.strip():
                    warning = '无可提取文字，请手动核对'
            except ImportError:
                warning = '当前 Python 缺少 pdfplumber，请手动填写信息'
            except Exception:
                warning = 'PDF 文字读取失败，请手动核对'
        else:
            warning = '图片票据，请手动填写信息'
        fields = parse_text(text, path.name)
        if reconstructed:
            fields['title'] = (reconstructed['items'][0]['category'] + ' · ' + reconstructed['items'][0]['product'])[:160]
        if not fields['seller'] and path.suffix.lower() == '.pdf':
            fields['seller'] = company_from_pdf(path)
        # 已有入库单数据是补充来源，优先用票面号码匹配，避免靠金额猜测。
        record = metadata.get(fields['invoice_no'])
        if record:
            fields['amount_cents'] = money(record.get('total'))
            items = record.get('items') or []
            if items:
                fields['title'] = (items[0].get('category', '') + ' · ' + items[0].get('product', ''))[:160]
        if fields['amount_cents'] is None or not fields['invoice_no'] or not fields['date']:
            warning = warning or '部分字段未识别，请核对补全'
        id_ = old['id'] if old else secrets.token_hex(12)
        if old:
            if old['manual'] and old['sha256'] != digest:
                warning = '原文件内容已变化，请重新核对台账'
            self.db.execute('UPDATE invoices SET sha256=?,mtime_ns=?,size=?,text=?,warning=?,page_count=?,updated_at=? WHERE id=?', (digest, stat.st_mtime_ns, stat.st_size, text, warning, page_count, now(), id_))
            if not old['manual']:
                self.db.execute('UPDATE invoices SET title=?,invoice_no=?,date=?,amount_cents=?,seller=? WHERE id=?', (*[fields[k] for k in ('title', 'invoice_no', 'date', 'amount_cents', 'seller')], id_))
            elif (not old['seller'] or company_needs_repair(old['seller'])) and fields['seller']:
                self.db.execute('UPDATE invoices SET seller=? WHERE id=?', (fields['seller'], id_))
            return 'updated'
        classification_source = source if source.startswith('手动导入') else str(self.root / source)
        status = '已报销' if '已报销' in Path(classification_source).parts else '待报销'
        values = dict(id=id_, path=str(path), filename=path.name, sha256=digest, mtime_ns=stat.st_mtime_ns, size=stat.st_size,
                      category=self.category(classification_source, text, fields['title'], path.name), status=status, text=text, warning=warning, page_count=page_count, source=source, created_at=now(), updated_at=now(), **fields)
        self.db.execute(f"INSERT INTO invoices({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
        self.event(id_, '导入', source)
        return 'added'

    def unpack(self, path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
        target = self.data_dir / 'imports' / digest
        result = []
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 300 or sum(e.file_size for e in entries) > MAX_ZIP:
                raise ValueError('ZIP 超过 300 个文件或解压总大小超过 200 MB')
            for entry in entries:
                if entry.is_dir():
                    continue
                name = entry.filename
                if not entry.flag_bits & 0x800:
                    try:
                        name = name.encode('cp437').decode('gb18030')
                    except (UnicodeError, LookupError):
                        pass
                if Path(name).suffix.lower() not in EXTENSIONS or entry.file_size > MAX_FILE:
                    continue
                # 扁平保存并加成员名摘要，无路径穿越、同名覆盖。
                name = hashlib.sha256(entry.filename.encode()).hexdigest()[:8] + '_' + safe_name(name)
                target.mkdir(exist_ok=True)
                output = target / name
                deleted = self.db.execute("SELECT id FROM invoices WHERE path=? AND deleted_at<>''", (str(output.resolve()),)).fetchone()
                if deleted:
                    continue
                if not output.exists():
                    content = archive.read(entry)
                    with output.open('xb') as f:
                        f.write(content)
                result.append(output)
        return result

    def scan(self):
        with self.lock:
            meta = self.metadata()
            counts = dict(added=0, updated=0, unchanged=0, skipped=0)
            errors = []
            paths = sorted(self.source_files(self.root), key=lambda p: str(p).lower())
            for path in paths:
                if self.ignored_path(path) or not path.is_file() or BASE in path.parents or self.data_dir in path.parents or path.is_symlink():
                    continue
                try:
                    # resolve 防止联接点带出票据根目录。
                    if not path.resolve().is_relative_to(self.root):
                        continue
                    source = str(path.relative_to(self.root))
                    if path.suffix.lower() == '.zip':
                        if path.stat().st_size > MAX_FILE:
                            raise ValueError('ZIP 超过 40 MB')
                        for item in self.unpack(path):
                            counts[self.ingest(item, source, meta)] += 1
                    elif path.suffix.lower() in EXTENSIONS:
                        counts[self.ingest(path, source, meta)] += 1
                except Exception as e:
                    errors.append(f'{path.name}: {type(e).__name__}: {e}')
            # 用户上传的票据也参与刷新。
            for path in (self.data_dir / 'imports').rglob('*'):
                if path.is_file() and path.suffix.lower() in EXTENSIONS:
                    old = self.db.execute('SELECT source FROM invoices WHERE path=?', (str(path.resolve()),)).fetchone()
                    if not old:
                        counts[self.ingest(path, '手动导入', meta)] += 1
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('last_scan', now()))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('parser_version', '3'))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('company_parser_version', '2'))
            self.db.commit()
            self.parser_changed = False
            self.company_parser_changed = False
            return dict(**counts, errors=errors)

    def upload(self, name, encoded):
        content = base64.b64decode(encoded, validate=True)
        if len(content) > MAX_FILE:
            raise ValueError('单个文件上限 40 MB')
        name = safe_name(name)
        if Path(name).suffix.lower() not in EXTENSIONS | {'.zip'}:
            raise ValueError('支持 PDF、PNG、JPG 和 ZIP')
        target = self.data_dir / 'imports' / hashlib.sha256(content).hexdigest()[:24]
        target.mkdir(exist_ok=True)
        path = target / name
        if not path.exists():
            with path.open('xb') as f:
                f.write(content)
        with self.lock:
            paths = self.unpack(path) if path.suffix.lower() == '.zip' else [path]
            counts = dict(added=0, unchanged=0, updated=0, skipped=0)
            for item in paths:
                counts[self.ingest(item, '手动导入 / ' + name, {})] += 1
            self.db.commit()
            return counts

    def documents(self):
        # 只展示已完成生成任务的成品，不扫描用户目录中的 Word 文件。
        return self.receipts.documents()

    def state(self):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM invoices WHERE deleted_at='' ORDER BY created_at,id")]
            rows = [row for row in rows if not self.ignored_invoice(row) and not is_itinerary(row)]
            rows.sort(key=lambda r: not Path(r['path']).is_file())
            hashes, numbers = {}, {}
            for row in rows:
                duplicate = hashes.get(row['sha256']) or (numbers.get(row['invoice_no']) if row['invoice_no'] else None)
                row['duplicate_of'] = duplicate
                hashes.setdefault(row['sha256'], row['id'])
                if row['invoice_no']:
                    numbers.setdefault(row['invoice_no'], row['id'])
                row['missing'] = not Path(row['path']).is_file()
                row['needs_review'] = bool(row['warning']) or row['amount_cents'] is None or not row['invoice_no'] or not row['date']
                row.pop('text')
            last = self.db.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
            events = [dict(r) for r in self.db.execute('SELECT e.*,i.title FROM events e LEFT JOIN invoices i ON i.id=e.invoice_id ORDER BY e.id DESC LIMIT 30')]
            trash = [dict(r) for r in self.db.execute("SELECT id,title,filename,path,invoice_no,status,deleted_at FROM invoices WHERE deleted_at<>'' AND purged_at='' ORDER BY deleted_at DESC,id")]
            return dict(invoices=rows, categories=list(CATEGORIES), trash=trash, documents=self.documents(), receipt_jobs=self.receipts.recent(), receipt_skill=self.receipts.availability(), root=str(self.root), managed_roots=[str(path) for path in self.managed_roots], root_available=self.root.is_dir(), data_dir=str(self.data_dir), last_scan=last[0] if last else '', events=events, key=self.key)

    def checked_ids(self, ids, max_count=2000):
        if not isinstance(ids, list) or not ids or (max_count is not None and len(ids) > max_count) or any(not isinstance(id_, str) for id_ in ids):
            raise ValueError('请选择需要处理的票据')
        return list(dict.fromkeys(ids))

    def invoice_path(self, value):
        path = Path(value)
        if path.is_symlink():
            raise ValueError('不支持处理符号链接文件')
        path = path.resolve()
        if not (any(path.is_relative_to(root) for root in self.managed_roots) or path.is_relative_to(self.data_dir / 'imports')):
            raise ValueError('票据路径不在管理目录内')
        return path

    def delete_duplicates(self, ids):
        return self._delete_invoices(ids, duplicates_only=True)

    def delete_invoices(self, ids):
        return self._delete_invoices(ids, duplicates_only=False)

    def _delete_invoices(self, ids, duplicates_only):
        ids = self.checked_ids(ids)
        with self.lock:
            if self.receipts.active.locked():
                raise ValueError('入库单正在生成，请等待完成后删除票据')
            active = {row['id']: row for row in self.state()['invoices']}
            plans = []
            for id_ in ids:
                row = active.get(id_)
                if not row:
                    raise ValueError('所选票据不存在或已被忽略，请刷新列表后重试')
                source = self.invoice_path(row['path'])
                if duplicates_only:
                    original = active.get(row['duplicate_of'])
                    if not original or original['duplicate_of'] or original['missing'] or original['id'] in ids:
                        raise ValueError('仅能删除仍有有效原票的重复副本，请刷新列表后重试')
                    original_path = self.invoice_path(original['path'])
                    if source == original_path:
                        raise ValueError('不能删除原票文件')
                    if hashlib.sha256(original_path.read_bytes()).hexdigest() != original['sha256']:
                        raise ValueError('原票文件已变化，请重新扫描后操作')
                target = None
                if source.exists():
                    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != row['sha256']:
                        raise ValueError('票据文件已变化，请重新扫描后操作')
                    target = self.data_dir / 'trash' / (id_ + '_' + secrets.token_hex(4)) / source.name
                    if not target.resolve().is_relative_to(self.data_dir / 'trash'):
                        raise ValueError('回收站路径不正确')
                plans.append((row, source, target))
            moved = []
            try:
                with self.db:
                    for row, source, target in plans:
                        if target:
                            target.parent.mkdir(parents=True, exist_ok=False)
                            source.rename(target)
                            moved.append((source, target))
                        self.db.execute('UPDATE invoices SET deleted_at=?,trash_path=?,updated_at=? WHERE id=?', (now(), str(target) if target else '', now(), row['id']))
                        self.event(row['id'], '删除重复副本' if duplicates_only else '删除票据', row['path'])
            except Exception:
                for source, target in reversed(moved):
                    if target.exists() and not source.exists():
                        target.rename(source)
                raise
            return {'deleted': len(plans)}

    def restore_duplicates(self, ids):
        return self.restore_invoices(ids)

    def restore_invoices(self, ids):
        ids = self.checked_ids(ids)
        with self.lock:
            plans = []
            for id_ in ids:
                row = self.db.execute("SELECT * FROM invoices WHERE id=? AND deleted_at<>'' AND purged_at=''", (id_,)).fetchone()
                if not row:
                    raise ValueError('回收站记录不存在，请刷新后重试')
                destination = self.invoice_path(row['path'])
                source = None
                if row['trash_path']:
                    source = Path(row['trash_path']).resolve()
                    if not source.is_relative_to(self.data_dir / 'trash') or not source.is_file():
                        raise ValueError('回收站文件不存在，无法恢复')
                    if destination.exists():
                        raise ValueError('原位置已有同名文件，恢复不会覆盖它，请先移走同名文件')
                    if hashlib.sha256(source.read_bytes()).hexdigest() != row['sha256']:
                        raise ValueError('回收站文件已变化，无法恢复')
                plans.append((row, source, destination))
            moved = []
            try:
                with self.db:
                    for row, source, destination in plans:
                        if source:
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            source.rename(destination)
                            moved.append((source, destination))
                        self.db.execute("UPDATE invoices SET deleted_at='',trash_path='',updated_at=? WHERE id=?", (now(), row['id']))
                        self.event(row['id'], '恢复票据', row['path'])
            except Exception:
                for source, destination in reversed(moved):
                    if destination.exists() and not source.exists():
                        destination.rename(source)
                raise
            return {'restored': len(plans)}

    def empty_trash(self, ids, confirmed=False):
        if confirmed is not True:
            raise ValueError('请先确认永久删除回收站票据，此操作无法恢复')
        ids = [] if ids == [] else self.checked_ids(ids, max_count=None)
        with self.lock:
            trash_root = self.data_dir / 'trash'
            if trash_root.is_symlink() or trash_root.resolve() != trash_root:
                raise ValueError('回收站目录不能是链接')
            plans, paths = [], set()
            # 先检查整个确认快照，避免旧窗口误删已恢复的票据。
            for id_ in ids:
                row = self.db.execute('SELECT * FROM invoices WHERE id=?', (id_,)).fetchone()
                if not row or not row['deleted_at']:
                    raise ValueError('回收站记录已变化，请刷新后重新确认')
                if row['purged_at']:
                    continue
                source = Path(row['trash_path']) if row['trash_path'] else None
                if source is not None:
                    resolved = source.resolve()
                    if resolved == trash_root or not resolved.is_relative_to(trash_root):
                        raise ValueError('回收站文件路径不正确')
                    for part in (source, *source.parents):
                        if part == trash_root:
                            break
                        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                            raise ValueError('回收站文件不能是链接')
                    if source.exists() and not source.is_file():
                        raise ValueError('回收站条目不是文件')
                    if resolved in paths:
                        raise ValueError('回收站文件关联冲突，请检查记录')
                    paths.add(resolved)
                    source = resolved
                plans.append((row, source))
            purged, failed = 0, []
            for row, source in plans:
                staged = None
                try:
                    if source is not None and source.exists():
                        staged = source.with_name('.purge-' + secrets.token_hex(16) + source.suffix)
                        source.rename(staged)
                    # 保留删除标记，防止扫描或重复导入 ZIP 复活已经清空的记录。
                    with self.db:
                        # 保留暂存路径：若进程在提交后中断，仍可定位尚未删除的文件。
                        self.db.execute("UPDATE invoices SET purged_at=?,trash_path=?,updated_at=? WHERE id=?", (now(), str(staged) if staged else '', now(), row['id']))
                        self.event(row['id'], '永久删除票据', row['filename'])
                except Exception as error:
                    if staged is not None and staged.exists():
                        try:
                            if source.exists():
                                raise FileExistsError('原回收站位置已被占用')
                            staged.rename(source)
                        except OSError:
                            raise ValueError(f'清空失败，文件仍保留在 {staged}，请检查文件权限') from error
                    failed.append({'id': row['id'], 'error': str(error) or '无法处理此票据'})
                    continue
                if staged is not None:
                    try:
                        staged.unlink()
                    except OSError as error:
                        # 文件占用时退回可恢复状态，只将真正删掉的文件计为成功。
                        retained = staged
                        try:
                            if not source.exists():
                                staged.rename(source)
                                retained = source
                        except OSError:
                            pass
                        try:
                            with self.db:
                                self.db.execute("UPDATE invoices SET purged_at='',trash_path=?,updated_at=? WHERE id=?", (str(retained), now(), row['id']))
                                self.event(row['id'], '永久删除失败', str(error))
                        except Exception as recovery_error:
                            raise ValueError(f'文件未删除，仍保留在 {retained}，但回收站记录更新失败，请检查数据库') from recovery_error
                        failed.append({'id': row['id'], 'error': str(error) or '文件被占用，请稍后重试'})
                        continue
                purged += 1
                if source is not None and source.parent != trash_root:
                    try:
                        source.parent.rmdir()  # 只清理本条目留下的空目录。
                    except OSError:
                        pass
            remaining = self.db.execute("SELECT COUNT(*) FROM invoices WHERE deleted_at<>'' AND purged_at=''").fetchone()[0]
            return {'purged': purged, 'failed': failed, 'remaining': remaining}

    def export_zip(self, ids):
        ids = self.checked_ids(ids)
        with self.lock:
            active = {row['id']: row for row in self.state()['invoices']}
            rows = []
            total_size = 0
            for id_ in ids:
                row = active.get(id_)
                if not row or row['missing']:
                    raise ValueError('所选票据文件不存在或已被忽略，请刷新后重试')
                path = self.invoice_path(row['path'])
                size = path.stat().st_size
                if not path.is_file() or size > MAX_FILE:
                    raise ValueError('所选票据不可读取或超过单文件 40 MB 上限')
                total_size += size
                if total_size > MAX_ZIP:
                    raise ValueError('单次 ZIP 导出的原文件总大小不能超过 200 MB，请分批导出')
                rows.append((row, path))
            output = io.BytesIO()
            actual_size = 0
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for index, (row, path) in enumerate(rows, 1):
                    content = path.read_bytes()
                    actual_size += len(content)
                    if len(content) > MAX_FILE or actual_size > MAX_ZIP:
                        raise ValueError('所选文件大小发生变化，请重新扫描后导出')
                    if hashlib.sha256(content).hexdigest() != row['sha256']:
                        raise ValueError('所选票据文件已变化，请重新扫描后导出')
                    # 序号避免同名覆盖；ZIP 不包含本机绝对路径。
                    archive.writestr(f'{index:04d}_{safe_name(row["filename"])}', content)
            return output.getvalue()

    def update(self, ids, patch):
        allowed = {'title', 'invoice_no', 'date', 'amount', 'category', 'seller', 'status', 'project', 'notes', 'reviewed'}
        ids = self.checked_ids(ids)
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError('无效的修改参数')
        values = {}
        for field, value in patch.items():
            if field == 'amount':
                values['amount_cents'] = money(value)
            elif field == 'reviewed':
                if value is True:
                    values['warning'] = ''
            else:
                if not isinstance(value, str) or len(value) > (4000 if field == 'notes' else 200):
                    raise ValueError('字段格式或长度不正确')
                values[field] = value.strip()
        if 'category' in values and values['category'] not in CATEGORIES:
            raise ValueError('请选择其他票据、出行交通、科研材料、铁路客票或住宿费')
        if 'status' in values and values['status'] not in STATUSES:
            raise ValueError('不支持的报销状态')
        if values.get('date'):
            datetime.strptime(values['date'], '%Y-%m-%d')
        values['manual'] = 1
        values['updated_at'] = now()
        with self.lock:
            before = {}
            for id_ in ids:
                row = self.db.execute("SELECT category FROM invoices WHERE id=? AND deleted_at=''", (id_,)).fetchone()
                if not row:
                    raise ValueError('票据不存在')
                before[id_] = row['category'] or ''
            # 类型变更及其审计记录必须一起提交，失败时一起回滚。
            with self.db:
                for id_ in ids:
                    self.db.execute('UPDATE invoices SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', (*values.values(), id_))
                    changed_type = 'category' in values and before[id_] != values['category']
                    if changed_type:
                        self.event(id_, '修改发票类型', json.dumps({'before': before[id_], 'after': values['category']}, ensure_ascii=False))
                    if set(patch) != {'category'}:
                        self.event(id_, '修改', json.dumps(patch, ensure_ascii=False))

    def category_history(self, id_):
        with self.lock:
            if not self.db.execute("SELECT id FROM invoices WHERE id=? AND deleted_at=''", (id_,)).fetchone():
                raise ValueError('票据不存在')
            records = self.db.execute("SELECT id,detail,created_at FROM events WHERE invoice_id=? AND action='修改发票类型' ORDER BY id DESC", (id_,)).fetchall()
            return [dict(id=row['id'], created_at=row['created_at'], **json.loads(row['detail'])) for row in records]

    def backup(self):
        with self.lock:
            folder = self.data_dir / 'backups'
            folder.mkdir(exist_ok=True)
            path = folder / ('台账备份_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(2) + '.sqlite3')
            with closing(sqlite3.connect(path)) as target:
                self.db.backup(target)
            return path


class LocalServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许同时绑定相同端口；工作台必须独占端口。
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = 'InvoiceWorkbench/1.0'

    @property
    def store(self):
        return self.server.store

    def log_message(self, format_, *args):
        # 不把发票字段写入控制台日志。
        pass

    def send(self, data, content_type='application/json; charset=utf-8', status=200, filename=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; object-src 'self'; connect-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host', '') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def do_GET(self):
        if not self.valid_host():
            return self.send({'error': '不允许的访问来源'}, status=403)
        request = urlparse(self.path)
        route, query = request.path, parse_qs(request.query)
        try:
            if route == '/api/health':
                # 就绪检查不读取台账、遍历文档或等待扫描/生成的数据库锁。
                return self.send({'app': 'piaoyu-invoice-workbench', 'data_dir': str(self.store.data_dir)})
            if route == '/api/state':
                return self.send(self.store.state())
            if route.startswith('/api/category-history/'):
                return self.send(self.store.category_history(route.rsplit('/', 1)[-1]))
            if route.startswith('/api/receipt-jobs/'):
                if route.endswith('/statistics'):
                    return self.send(self.store.receipts.statistics_data(route.split('/')[-2]))
                if route.endswith('/manual'):
                    return self.send(self.store.receipts.manual_data(route.split('/')[-2]))
                return self.send(self.store.receipts.get(route.rsplit('/', 1)[-1]))
            if route.startswith('/api/preview/'):
                id_ = route.rsplit('/', 1)[-1]
                with self.store.lock:
                    row = self.store.db.execute("SELECT path,sha256,page_count FROM invoices WHERE id=? AND deleted_at=''", (id_,)).fetchone()
                if not row or not Path(row['path']).is_file():
                    return self.send({'error': '票据文件不存在'}, status=404)
                page = int(query.get('page', ['1'])[0])
                if page < 1 or page > row['page_count']:
                    return self.send({'error': '页码超出范围'}, status=400)
                cache_dir = self.store.data_dir / 'previews'
                cache_dir.mkdir(exist_ok=True)
                output = cache_dir / (row['sha256'] + '_' + str(page) + '.png')
                with self.store.lock:
                    if not output.exists():
                        import pdfplumber
                        with pdfplumber.open(row['path']) as pdf:
                            with PDFIUM_LOCK:
                                picture = pdf.pages[page - 1].to_image(resolution=125).original
                            picture.save(output, format='PNG')
                return self.send(output.read_bytes(), 'image/png')
            if route.startswith('/api/file/'):
                id_ = route.rsplit('/', 1)[-1]
                with self.store.lock:
                    row = self.store.db.execute("SELECT path,filename FROM invoices WHERE id=? AND deleted_at=''", (id_,)).fetchone()
                if not row or not Path(row['path']).is_file():
                    return self.send({'error': '票据文件不存在'}, status=404)
                file = Path(row['path'])
                return self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream', filename=file.name if 'download' in query else None)
            if route.startswith('/api/document/'):
                id_ = route.rsplit('/', 1)[-1]
                documents = self.store.documents()
                downloads = documents + [doc['statistics'] for doc in documents if doc.get('statistics')]
                doc = next((d for d in downloads if d['id'] == id_), None)
                if not doc:
                    return self.send({'error': '文档不存在'}, status=404)
                return self.send(Path(doc['path']).read_bytes(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', filename=doc['name'])
            if route == '/api/export':
                rows = self.store.state()['invoices']
                ids = set(query.get('ids', [''])[0].split(',')) if query.get('ids') else None
                out = io.StringIO(newline='')
                writer = csv.writer(out)
                writer.writerow(['品名', '发票号码', '开票日期', '价税合计', '分类', '报销状态', '所属项目', '销售方', '备注', '原始路径', '重复票据'])
                def cell(v):
                    s = str(v or '')
                    return "'" + s if s.startswith(('=', '+', '-', '@', '\t', '\r')) or (s.isdigit() and len(s) > 12) else s
                for row in rows:
                    if ids is not None and row['id'] not in ids:
                        continue
                    fields = [row['title'], row['invoice_no'], row['date'], '' if row['amount_cents'] is None else f"{row['amount_cents']/100:.2f}", row['category'], row['status'], row['project'], row['seller'], row['notes'], row['path'], '是' if row['duplicate_of'] else '否']
                    writer.writerow([cell(v) for v in fields])
                return self.send(b'\xef\xbb\xbf' + out.getvalue().encode(), 'text/csv; charset=utf-8', filename='发票台账.csv')
            assets = {'/': 'index.html', '/app.js': 'app.js', '/receipts.js': 'receipts.js', '/statistics.js': 'statistics.js', '/folders.js': 'folders.js', '/categories.js': 'categories.js', '/themes.js': 'themes.js', '/themes.css': 'themes.css', '/style.css': 'style.css', '/favicon.svg': 'favicon.svg'}
            if route in assets:
                path = BASE / 'web' / assets[route]
                return self.send(path.read_bytes(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            return self.send({'error': '页面不存在'}, status=404)
        except (OSError, ValueError):
            self.send({'error': '读取失败，请检查本地文件'}, status=400)

    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-Workbench-Key') != self.store.key:
            return self.send({'error': '请从本地工作台发起操作'}, status=403)
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}:
            return self.send({'error': '不允许的访问来源'}, status=403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 58 * 1024 * 1024:
                return self.send({'error': '请求过大，单个文件上限 40 MB'}, status=413)
            body = json.loads(self.rfile.read(length) or b'{}')
            if self.path == '/api/pick-folder':
                return self.send(pick_folder(self.store.root))
            if self.path == '/api/settings/invoice-folder':
                return self.send(self.store.set_invoice_root(body.get('path')))
            if self.path == '/api/scan':
                return self.send(self.store.scan())
            if self.path == '/api/receipt-jobs':
                return self.send(self.store.receipts.create(body.get('ids')))
            if self.path.startswith('/api/receipt-jobs/') and self.path.endswith('/statistics'):
                return self.send(self.store.receipts.save_statistics(self.path.split('/')[-2], body.get('rows')))
            if self.path.startswith('/api/receipt-jobs/') and self.path.endswith('/manual'):
                return self.send(self.store.receipts.save_manual(self.path.split('/')[-2], body.get('invoices'), resume=body.get('resume') is True))
            if self.path == '/api/update':
                self.store.update(body.get('ids'), body.get('patch'))
                return self.send({'ok': True})
            if self.path == '/api/delete-duplicates':
                return self.send(self.store.delete_duplicates(body.get('ids')))
            if self.path == '/api/delete-invoices':
                return self.send(self.store.delete_invoices(body.get('ids')))
            if self.path == '/api/restore-duplicates':
                return self.send(self.store.restore_duplicates(body.get('ids')))
            if self.path == '/api/restore-invoices':
                return self.send(self.store.restore_invoices(body.get('ids')))
            if self.path == '/api/empty-trash':
                return self.send(self.store.empty_trash(body.get('ids'), body.get('confirmed')))
            if self.path == '/api/export-zip':
                return self.send(self.store.export_zip(body.get('ids')), 'application/zip', filename='发票原件.zip')
            if self.path == '/api/import':
                return self.send(self.store.upload(body['name'], body['content']))
            if self.path == '/api/backup':
                path = self.store.backup()
                return self.send(path.read_bytes(), 'application/octet-stream', filename=path.name)
            if self.path == '/api/open-folder':
                os.startfile(str(self.store.root))
                return self.send({'ok': True})
            if self.path == '/api/shutdown':
                if self.store.receipts.active.locked():
                    raise ValueError('入库单正在生成，请等待完成后停止服务')
                self.send({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.send({'error': '操作不存在'}, status=404)
        except (ValueError, KeyError, TypeError, zipfile.BadZipFile) as e:
            self.send({'error': str(e) or '参数错误'}, status=400)
        except Exception:
            self.send({'error': '操作失败，请检查文件权限或重启工作台'}, status=500)


def main():
    global IGNORED_DIRECTORIES
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18626)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--ignore-dir', action='append', default=[], help='不参与扫描的完整目录路径，可重复指定')
    args = parser.parse_args()
    IGNORED_DIRECTORIES = tuple(Path(value).expanduser().resolve() for value in args.ignore_dir)
    # 先取得端口再初始化数据库，重复启动不能重置正在运行的生成任务。
    try:
        server = LocalServer(('127.0.0.1', args.port), Handler)
    except OSError:
        import urllib.request
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{args.port}/api/state', timeout=2) as response:
                state = json.load(response)
            if state.get('data_dir') == str((BASE / 'data').resolve()):
                if not args.no_browser:
                    webbrowser.open(f'http://127.0.0.1:{args.port}')
                return
        except Exception:
            pass
        raise SystemExit(f'端口 {args.port} 已被其他程序占用，请使用 --port 指定其他端口。')
    store = None
    try:
        store = Store(BASE.parent / 'invoices', BASE / 'data')
        server.store = store
        server.daemon_threads = True
        store.scan()
        url = f'http://127.0.0.1:{server.server_port}'
        print('Invoice Workbench: ' + url, flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if store is not None:
            store.db.close()


if __name__ == '__main__':
    main()
