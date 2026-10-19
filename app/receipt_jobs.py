"""调用已安装的 ruku 三步流程，在副本目录生成科研材料入库单。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
from receipt_layout import clean_receipt, blank_receipt_fields

MAX_RECEIPTS = 5
LAYOUT_VERSION = 3


def timestamp():
    return datetime.now().astimezone().isoformat(timespec='microseconds')


class ReceiptJobs:
    def __init__(self, store, skill_dir=None):
        self.store = store
        self.skill_dir = Path(skill_dir) if skill_dir else Path(__file__).resolve().parent / 'vendor' / 'ruku'
        self.directory = store.data_dir / 'receipts'
        self.template = Path(__file__).resolve().parent / 'assets' / '入库单模板.docx'
        self.active = threading.Lock()
        self.thread = None
        self.retention_warning = ''
        self.layout_warnings = []
        with store.lock:
            store.db.execute('CREATE TABLE IF NOT EXISTS receipt_jobs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL)')
            # 中断的任务保留记录和副本，用户可以重新生成。
            for row in store.db.execute('SELECT id,payload FROM receipt_jobs').fetchall():
                job = json.loads(row['payload'])
                if job['status'] == 'running':
                    job.update(status='failed', error='上次生成因服务退出而中断，请重新生成。')
                    store.db.execute('UPDATE receipt_jobs SET payload=? WHERE id=?', (json.dumps(job, ensure_ascii=False), row['id']))
            store.db.commit()
        try:
            self.prune()
        except (OSError, ValueError) as error:
            self.retention_warning = f'旧入库单未能清理，请关闭占用文件后重启工作台：{error}'
        # 同步修正已生成的工作台入库单，不处理目录外的用户文档。
        with store.lock:
            completed = [json.loads(row['payload']) for row in store.db.execute('SELECT payload FROM receipt_jobs')]
        for job in completed:
            if job['status'] == 'done' and job.get('layout_version', 0) < LAYOUT_VERSION:
                try:
                    output = self._job_directory(job) / '发票' / '入库单_已填写.docx'
                    if output.is_file():
                        clean_receipt(output, blank_rows_from=self.template)
                        job['layout_version'] = LAYOUT_VERSION
                        self._save(job)
                except Exception as error:
                    # 单份旧文档被占用或损坏，不影响台账、预览和其他入库单。
                    self.layout_warnings.append(f'旧入库单 {job["id"]} 版式未能更新，请关闭文件后重启：{error}')

    def _job_directory(self, job):
        if not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}', job['id']):
            raise ValueError('入库单任务目录无效')
        root = self.directory.resolve()
        work = (self.directory / job['id']).resolve()
        expected = work / '发票' / '入库单_已填写.docx'
        if work != root / job['id'] or work.parent != root or Path(job['output']).resolve() != expected:
            raise ValueError('入库单任务路径超出生成目录')
        if any(Path(source['path']).resolve().is_relative_to(work) for source in job.get('sources', [])):
            raise ValueError('入库单任务目录包含原票，不能清理')
        return work

    def prune(self):
        """只清理生成器自己的旧任务目录；实际文件和下载记录同时删除。"""
        with self.store.lock:
            jobs = [json.loads(row['payload']) for row in self.store.db.execute('SELECT payload FROM receipt_jobs ORDER BY created_at DESC,id DESC')]
            completed = [job for job in jobs if job['status'] == 'done']
            for job in completed[MAX_RECEIPTS:]:
                work = self._job_directory(job)
                if work.exists():
                    def linked(path):
                        return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                    if linked(work) or any(linked(path) for path in work.rglob('*')):
                        raise ValueError('入库单任务目录含链接，不能自动清理')
                    shutil.rmtree(work)
                self.store.db.execute('DELETE FROM receipt_jobs WHERE id=?', (job['id'],))
                self.store.db.commit()
            for job in completed[:MAX_RECEIPTS]:
                if job.pop('retention_error', None):
                    self._save(job)
            self.retention_warning = ''

    def availability(self):
        paths = [self.skill_dir / 'scripts' / name for name in ('extract_invoices.py', 'rename_files.py', 'fill_receipt.py')]
        paths.append(self.template)
        if not all(path.is_file() for path in paths):
            return {'available': False, 'reason': '未找到完整的内置入库单脚本或模板，请重新下载完整源码'}
        if any(importlib.util.find_spec(name) is None for name in ('pdfplumber', 'docx')):
            return {'available': False, 'reason': '本地 Python 缺少 pdfplumber 或 python-docx'}
        return {'available': True, 'reason': '', 'maintenance_warning': '；'.join(filter(None, [self.retention_warning, *self.layout_warnings]))}

    def get(self, id_):
        with self.store.lock:
            row = self.store.db.execute('SELECT payload FROM receipt_jobs WHERE id=?', (id_,)).fetchone()
            if not row:
                raise ValueError('入库单生成任务不存在')
            return self._with_manual_flag(json.loads(row['payload']))

    def recent(self):
        with self.store.lock:
            return [self._with_manual_flag(json.loads(row['payload'])) for row in self.store.db.execute('SELECT payload FROM receipt_jobs ORDER BY created_at DESC,id DESC LIMIT 10')]

    def _with_manual_flag(self, job):
        job['manual_available'] = job['status'] == 'failed' and job.get('stage') == 1 and all(
            (Path(job['output']).parent / f'{index:03d}.pdf').is_file()
            for index in range(1, len(job['sources']) + 1))
        return job

    def manual_data(self, id_):
        job = self.get(id_)
        if not job['manual_available']:
            raise ValueError('该任务当前不能补填，请在提取失败后操作')
        self._job_directory(job)
        if 'manual_draft' in job:
            return {'job_id': id_, 'invoices': job['manual_draft']}
        extracted = {}
        try:
            content = json.loads(Path(job['data_path']).read_text(encoding='utf-8'))
            if isinstance(content, list):
                extracted = {inv.get('original_filename'): inv for inv in content if isinstance(inv, dict)}
        except (OSError, ValueError):
            pass
        with self.store.lock:
            records = {row['id']: dict(row) for row in self.store.db.execute('SELECT id,invoice_no,amount_cents FROM invoices')}
        invoices = []
        for index, source in enumerate(job['sources'], 1):
            inv = extracted.get(f'{index:03d}.pdf', {})
            row = records.get(source['id'], {})
            total = inv.get('total')
            if total is None and row.get('amount_cents') is not None:
                total = f"{row['amount_cents']/100:.2f}"
            items = inv.get('items') or [{}]
            invoices.append(dict(source_id=source['id'], filename=source['filename'],
                invoice_no=inv.get('invoice_no') or row.get('invoice_no', ''), total='' if total is None else str(total),
                extracted_complete=bool(inv.get('invoice_no') and inv.get('total') is not None and inv.get('items')),
                items=[{field: '' if item.get(field) is None else str(item.get(field, ''))
                        for field in ('category', 'product', 'spec', 'unit', 'quantity', 'unit_price', 'amount', 'tax')}
                       for item in items if isinstance(item, dict)]))
        return {'job_id': id_, 'invoices': invoices}

    def _manual_invoices(self, job, submitted, complete):
        if not isinstance(submitted, list) or len(submitted) != len(job['sources']):
            raise ValueError('请补填本次任务的全部发票')
        by_id = {}
        for inv in submitted:
            if not isinstance(inv, dict) or inv.get('source_id') in by_id:
                raise ValueError('补填发票清单无效')
            by_id[inv.get('source_id')] = inv
        if set(by_id) != {source['id'] for source in job['sources']}:
            raise ValueError('补填发票与原任务不一致')

        def text(value, label, limit=100):
            if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
                raise ValueError(f'{label}格式不正确，最多 {limit} 字')
            value = value.strip()
            return value

        def number(value, label, positive=False, places=2):
            try:
                decimal = Decimal(value)
                if not decimal.is_finite() or decimal < 0 or decimal > 100000000 or (positive and decimal == 0):
                    raise ValueError()
                if places is not None and decimal != decimal.quantize(Decimal(1).scaleb(-places)):
                    raise ValueError()
                return decimal
            except (InvalidOperation, ValueError):
                precision = f'，最多 {places} 位小数' if places is not None else ''
                raise ValueError(f'{label}需为有效的{"正" if positive else "非负"}数{precision}')

        draft, normalized, invoice_nos = [], [], set()
        item_count = 0
        for index, source in enumerate(job['sources'], 1):
            inv = by_id[source['id']]
            prefix = f'第 {index} 张发票'
            invoice_no = text(inv.get('invoice_no', ''), prefix + '号码', 80)
            total_text = text(inv.get('total', ''), prefix + '价税合计', 40)
            items = inv.get('items')
            if not isinstance(items, list) or not 1 <= len(items) <= 200:
                raise ValueError(prefix + '需填写 1 至 200 项商品')
            item_count += len(items)
            if item_count > 2000:
                raise ValueError('单次最多补填 2000 项商品')
            clean_items, output_items = [], []
            for row_index, item in enumerate(items, 1):
                if not isinstance(item, dict):
                    raise ValueError(prefix + '商品格式错误')
                label = f'{prefix}第 {row_index} 项'
                clean = {field: text(item.get(field, ''), label + name, limit)
                         for field, name, limit in (
                             ('category', '商品类别', 40), ('product', '品名', 100),
                             ('spec', '规格', 100), ('unit', '单位', 20),
                             ('quantity', '数量', 40), ('unit_price', '单价', 40),
                             ('amount', '不含税金额', 40), ('tax', '税额', 40))}
                clean_items.append(clean)
                if not complete:
                    continue
                # 原始重命名脚本用首项品名组成文件名，禁止目录及 Windows 非法字符。
                if any(c in clean['category'] + clean['product'] for c in '<>:"/\\|?*'):
                    raise ValueError(label + '类别或品名不能包含文件名特殊字符（规格可填写在规格栏）')
                quantity = number(clean['quantity'], label + '数量', True, 6) if clean['quantity'] else None
                amount = number(clean['amount'], label + '不含税金额') if clean['amount'] else None
                tax = number(clean['tax'], label + '税额') if clean['tax'] else None
                price = number(clean['unit_price'], label + '单价', places=None) if clean['unit_price'] else (
                    amount / quantity if amount is not None and quantity is not None else None)
                output_items.append({**clean, 'quantity': float(quantity) if quantity is not None else None,
                                     'unit_price': float(price) if price is not None else None,
                                     'amount': float(amount) if amount is not None else None,
                                     'tax': float(tax) if tax is not None else None, 'tax_rate': ''})
            draft.append(dict(source_id=source['id'], filename=source['filename'], invoice_no=invoice_no,
                              total=total_text, extracted_complete=bool(inv.get('extracted_complete')), items=clean_items))
            if complete:
                total = number(total_text, prefix + '价税合计') if total_text else None
                if invoice_no and invoice_no in invoice_nos:
                    raise ValueError('发票号码重复，请核对后继续')
                if invoice_no:
                    invoice_nos.add(invoice_no)
                # 单商品按票面价税合计填写；多商品也只有金额、税额均已知才核对。
                item_total = sum((Decimal(item['amount']) + Decimal(item['tax']) for item in clean_items), Decimal(0)) if len(clean_items) > 1 and all(item['amount'] and item['tax'] for item in clean_items) else None
                if total is not None and item_total is not None and abs(item_total - total) > Decimal('0.01'):
                    raise ValueError(prefix + '商品金额与税额之和不等于价税合计，请核对')
                normalized.append(dict(invoice_no=invoice_no, total=float(total) if total is not None else None,
                                       original_filename=f'{index:03d}.pdf', items=output_items))
        return draft, normalized

    @staticmethod
    def _amount_summary(job):
        return '金额留空' if job['total_cents'] is None else f"¥{job['total_cents']/100:.2f}"

    def _manual_script_data(self, job, data):
        """原脚本需要数字，用独立兼容数据运行；正式 JSON 保留真实空值。"""
        invoices = json.loads(data.read_text(encoding='utf-8'))
        render, rename, blank_cells = [], [], []
        for index, inv in enumerate(invoices, 1):
            item_amounts = [item['amount'] + (item.get('tax') or 0) if item.get('amount') is not None else None for item in inv['items']]
            subtotal = sum(item_amounts) if all(value is not None for value in item_amounts) else 0
            # 仅供脚本按来源分组；不能让多张无票号发票被当作同一张去重。
            compatible = {**inv, 'invoice_no':f'local-source-{index}', 'total':inv['total'] if inv['total'] is not None else subtotal,
                          'items':[]}
            for item in inv['items']:
                blanks = []
                if not item['category'] and not item['product']:
                    blanks.append(2)
                for field, col in (('unit',4),('quantity',5),('unit_price',6)):
                    if item.get(field) in (None, ''):
                        blanks.append(col)
                if (len(inv['items']) == 1 and inv['total'] is None and item.get('amount') is None) or (len(inv['items']) > 1 and item.get('amount') is None):
                    blanks.append(7)
                blank_cells.append(blanks)
                compatible['items'].append({**item, **{field: item.get(field) or 0 for field in ('quantity','unit_price','amount','tax')}})
            render.append(compatible)
            rename.append({**compatible, 'total':inv['total'] if inv['total'] is not None else '金额未填'})
        render_path = data.with_name('invoice_data_render.json')
        rename_path = data.with_name('invoice_data_rename.json')
        for path, content in ((render_path,render),(rename_path,rename)):
            path.write_text(json.dumps(content,ensure_ascii=False,indent=2),encoding='utf-8')
        return rename_path, render_path, blank_cells

    def save_manual(self, id_, invoices, resume=False, background=True):
        if not self.active.acquire(blocking=False):
            raise ValueError('已有入库单正在生成，请等待完成')
        handed_off = False
        try:
            job = self.get(id_)
            if not job['manual_available']:
                raise ValueError('该任务当前不能补填，请在提取失败后操作')
            self._job_directory(job)
            draft, normalized = self._manual_invoices(job, invoices, resume)
            for index, source in enumerate(job['sources'], 1):
                copy = Path(job['output']).parent / f'{index:03d}.pdf'
                if hashlib.sha256(copy.read_bytes()).hexdigest() != source['sha256']:
                    raise ValueError('任务中的发票副本已变化，请重新生成')
            job['manual_draft'] = draft
            job['manual_saved_at'] = timestamp()
            if resume:
                if not self.availability()['available']:
                    raise ValueError('未找到生成所需的本地技能或依赖')
                data = Path(job['data_path'])
                data.parent.mkdir(parents=True, exist_ok=True)
                # 识别原文只备份一次，补填内容和草稿均留在本地任务目录。
                original_data = data.with_name('invoice_data_extracted.json')
                if data.is_file() and not original_data.exists():
                    shutil.copy2(data, original_data)
                data.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding='utf-8')
                job.update(status='running', error='', item_count=sum(len(inv['items']) for inv in normalized),
                           total_cents=sum(int(Decimal(str(inv['total'])) * 100) for inv in normalized) if all(inv['total'] is not None for inv in normalized) else None)
                job['steps'][0].update(status='done', summary=f"按当前信息继续：{len(normalized)} 张发票、{job['item_count']} 项商品，{self._amount_summary(job)}；缺失字段留空")
            self._save(job, audit=('补填入库单', f"任务 {id_}，{'保存并继续生成' if resume else '保存草稿'}，{len(draft)} 张发票"))
            if resume:
                if background:
                    handed_off = self._start(job, resume=True)
                else:
                    handed_off = True
                    self._run(job, True)
            return self.get(id_)
        finally:
            if not handed_off:
                self.active.release()

    def _save(self, job, audit=None):
        job['updated_at'] = timestamp()
        with self.store.lock:
            try:
                self.store.db.execute('INSERT OR REPLACE INTO receipt_jobs VALUES(?,?,?)', (job['id'], json.dumps(job, ensure_ascii=False), job['created_at']))
                if audit is not None:
                    self.store.event(None, *audit)
                self.store.db.commit()
            except Exception:
                self.store.db.rollback()
                raise

    def _start(self, job, resume=False):
        """线程未启动时保留可重试的失败状态，锁仍由调用者释放。"""
        try:
            self.thread = threading.Thread(target=self._run, args=(job, resume), daemon=True)
            self.thread.start()
            return True
        except Exception as error:
            job.update(status='failed', error=f'无法启动生成任务，请重试：{error}')
            self._save(job)
            return False

    def documents(self):
        with self.store.lock:
            jobs = [json.loads(row['payload']) for row in self.store.db.execute('SELECT payload FROM receipt_jobs ORDER BY created_at DESC')]
        result = []
        for job in jobs:
            if job['status'] != 'done':
                continue
            path = Path(job['output']).resolve()
            if not path.is_relative_to(self.directory.resolve()) or not path.is_file():
                continue
            result.append(dict(id=job['document_id'], name=path.name, path=str(path), relative=str(path.relative_to(self.store.root)) if path.is_relative_to(self.store.root) else str(path), size=path.stat().st_size, generated=True, created_at=job['created_at'], invoice_count=job['invoice_count'], item_count=job['item_count'], total_cents=job['total_cents']))
        return result

    def create(self, ids, background=True):
        ids = self.store.checked_ids(ids)
        if len(ids) > 200:
            raise ValueError('单次最多生成 200 张发票的入库单')
        available = self.availability()
        if not available['available']:
            raise ValueError(available['reason'])
        if not self.active.acquire(blocking=False):
            raise ValueError('已有入库单正在生成，请等待完成')
        try:
            with self.store.lock:
                records = {row['id']: row for row in self.store.state()['invoices']}
                sources = []
                for id_ in ids:
                    row = records.get(id_)
                    if not row or row['duplicate_of'] or row['missing'] or row['category'] != '科研材料' or Path(row['path']).suffix.lower() != '.pdf':
                        raise ValueError('只能选择有效、非重复的科研材料 PDF 发票')
                    path = self.store.invoice_path(row['path'])
                    sources.append({'id': id_, 'path': str(path), 'filename': row['filename'], 'sha256': row['sha256']})
            job_id = datetime.now().strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(4)
            work = self.directory / job_id
            job = dict(id=job_id, status='running', stage=0, created_at=timestamp(), updated_at=timestamp(), sources=sources, invoice_count=len(sources), item_count=0, total_cents=0, chinese_total='', error='', output=str(work / '发票' / '入库单_已填写.docx'), data_path=str(work / '入库单数据' / 'invoice_data.json'), steps=[{'title': name, 'status': 'pending', 'summary': '', 'log': ''} for name in ('提取发票信息', '重命名发票副本', '生成入库单')])
            self._save(job)
            if background:
                if not self._start(job):
                    self.active.release()
            else:
                self._run(job)
            return self.get(job_id)
        except Exception:
            if self.active.locked():
                self.active.release()
            raise

    def _run(self, job, resume=False):
        try:
            invoice_dir = Path(job['output']).parent
            if not resume:
                invoice_dir.mkdir(parents=True, exist_ok=False)
            # 输入清单及重命名仅作用于独立任务副本，不改写台账与原票路径。
            for index, source in enumerate(job['sources'] if not resume else [], 1):
                path = self.store.invoice_path(source['path'])
                if hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
                    raise ValueError('所选发票文件已变化，请重新扫描后生成')
                shutil.copy2(path, invoice_dir / f'{index:03d}.pdf')
            data = Path(job['data_path'])
            scripts = self.skill_dir / 'scripts'
            rename_data, render_data, blank_cells = self._manual_script_data(job, data) if resume else (data, data, [])
            commands = [
                [str(scripts / 'extract_invoices.py'), '--invoice_dir', str(invoice_dir), '--output', str(data)],
                [str(scripts / 'rename_files.py'), '--invoice_dir', str(invoice_dir), '--data', str(rename_data)],
                [str(scripts / 'fill_receipt.py'), '--template', str(self.template), '--data', str(render_data), '--output', job['output']]
            ]
            environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
            for index, args in enumerate(commands):
                if resume and index == 0:
                    continue
                job['stage'] = index + 1
                step = job['steps'][index]
                step['status'] = 'running'
                self._save(job)
                result = subprocess.run([sys.executable, *args], cwd=invoice_dir.parent, env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                step['log'] = (result.stdout + '\n' + result.stderr)[-30000:]
                if result.returncode:
                    raise ValueError(f"第 {index + 1} 步失败：{(result.stderr or result.stdout).strip()[-800:]}")
                if index == 0:
                    invoices = json.loads(data.read_text(encoding='utf-8'))
                    # 不接受脚本缺失字段时的空白或 0 元占位结果。
                    if len(invoices) != len(job['sources']) or any(not inv.get('invoice_no') or inv.get('total') is None or not inv.get('items') for inv in invoices):
                        raise ValueError('部分发票未识别出完整票号、商品或金额，请补填发票信息后继续生成')
                    if len({inv['invoice_no'] for inv in invoices}) != len(invoices):
                        raise ValueError('识别结果包含相同票号，请核对后重试')
                    job['item_count'] = sum(len(inv['items']) for inv in invoices)
                    job['total_cents'] = sum(int(Decimal(str(inv['total'])) * 100) for inv in invoices)
                    step['summary'] = f"识别 {len(invoices)} 张发票、{job['item_count']} 项商品，合计 ¥{job['total_cents']/100:.2f}"
                elif index == 1:
                    step['summary'] = f"已重命名 {job['invoice_count']} 份发票副本"
                else:
                    if not Path(job['output']).is_file():
                        raise ValueError('生成脚本未产出入库单文件')
                    if resume:
                        originals = json.loads(data.read_text(encoding='utf-8'))
                        product_labels = [item['product'] if not item['category'] else None for inv in originals for item in inv['items']]
                        blank_receipt_fields(job['output'], blank_cells, blank_total=job['total_cents'] is None, product_labels=product_labels)
                    match = re.search(r'^Chinese:\s*(.+)$', result.stdout, re.M)
                    job['chinese_total'] = match.group(1).strip() if match and job['total_cents'] is not None else ''
                    job['document_id'] = hashlib.sha256(job['output'].encode()).hexdigest()[:24]
                    step['summary'] = f"已生成 {job['item_count']} 项商品，{self._amount_summary(job)}"
                step['status'] = 'done'
                self._save(job)
            job['status'] = 'done'
            job['layout_version'] = LAYOUT_VERSION
            self._save(job)
            try:
                self.prune()
            except (OSError, ValueError) as error:
                job['retention_error'] = f'旧入库单未能清理，请关闭占用文件后重启工作台：{error}'
                self.retention_warning = job['retention_error']
                self._save(job)
            with self.store.lock:
                self.store.event(None, '生成入库单', f"{job['invoice_count']} 张发票，{job['item_count']} 项商品，{self._amount_summary(job)}")
                self.store.db.commit()
        except Exception as error:
            job['status'] = 'failed'
            job['error'] = '生成超过 120 秒，请稍后重试' if isinstance(error, subprocess.TimeoutExpired) else str(error)
            for step in job['steps']:
                if step['status'] == 'running':
                    step['status'] = 'failed'
            self._save(job)
        finally:
            self.active.release()
