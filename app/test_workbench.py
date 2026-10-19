"""隔离临时文件的核心回归验证，不修改真实台账。"""
import base64
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import closing

from server import BASE, Handler, LocalServer, Store, ThreadingHTTPServer, money, parse_text


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, self.root / 'app' / 'data')
        self.store.scan()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def add_picture(self, name='receipt.png', content=b'picture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.store.scan()
        return next(r for r in self.store.state()['invoices'] if r['path'] == str(path.resolve()))

    def test_money_and_metadata(self):
        self.assertEqual(money('1,234.56'), 123456)
        self.assertEqual(money('0'), 0)
        self.assertIsNone(money(''))
        for value in ['NaN', 'Infinity', 'oops']:
            with self.assertRaises(ValueError):
                money(value)
        fields = parse_text('发票号码：\n开票日期：\n12345678901234567890\n2026年09月21日\n（小写）¥ 24.81', 'invoice.pdf')
        self.assertEqual(fields['invoice_no'], '12345678901234567890')
        self.assertEqual(fields['date'], '2026-09-21')
        self.assertEqual(fields['amount_cents'], 2481)
        ambiguous = parse_text('12345678901234567890\n12345678901234567891\n2026年09月21日\n2026年09月22日', 'invoice.pdf')
        self.assertEqual(ambiguous['invoice_no'], '')
        self.assertEqual(ambiguous['date'], '')
        railway = parse_text('电子发票（铁路电子客票）\n开票日期:2026年10月12日\n￥4.00', 'rail.pdf')
        self.assertEqual(railway['amount_cents'], 400)

    def test_manual_fields_persist_on_rescan_and_restart(self):
        row = self.add_picture('已报销/a.png')
        self.assertEqual(row['status'], '已报销')
        self.store.update([row['id']], {'title': '试剂', 'project': '项目A', 'notes': '已核对', 'amount': '12.34', 'status': '报销中'})
        self.store.scan()
        self.store.db.close()
        self.store = Store(self.root, self.root / 'app' / 'data')
        self.store.scan()
        saved = self.store.state()['invoices'][0]
        self.assertEqual(saved['project'], '项目A')
        self.assertEqual(saved['amount_cents'], 1234)
        self.assertEqual(saved['status'], '报销中')
        self.assertEqual(saved['notes'], '已核对')

    def test_duplicates_and_moved_files(self):
        row = self.add_picture()
        self.store.update([row['id']], {'project': '移动后保留'})
        moved = self.root / 'moved.png'
        Path(row['path']).rename(moved)
        self.store.scan()
        state = self.store.state()['invoices']
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]['id'], row['id'])
        self.assertEqual(state[0]['project'], '移动后保留')
        self.add_picture('copy.png')
        state = self.store.state()['invoices']
        self.assertEqual(sum(bool(r['duplicate_of']) for r in state), 1)
        self.assertEqual(sum(not r['duplicate_of'] for r in state), 1)

    def test_zip_flatten_and_repeat_upload(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('../../outside.png', b'picture1')
            archive.writestr('folder/second.png', b'picture2')
            archive.writestr('ignored.exe', b'not executable')
        encoded = base64.b64encode(buffer.getvalue()).decode()
        self.assertEqual(self.store.upload('receipts.zip', encoded)['added'], 2)
        self.assertEqual(self.store.upload('receipts.zip', encoded)['unchanged'], 2)
        self.assertFalse((self.root.parent / 'outside.png').exists())
        for row in self.store.state()['invoices']:
            self.assertTrue(Path(row['path']).is_relative_to(self.store.data_dir))
        with self.assertRaises(ValueError):
            self.store.upload('bad.exe', base64.b64encode(b'test').decode())

    def test_invalid_batch_is_atomic(self):
        row = self.add_picture()
        with self.assertRaises(ValueError):
            self.store.update([row['id'], 'missing'], {'status': '已报销'})
        self.assertEqual(self.store.state()['invoices'][0]['status'], '待报销')

    def test_backup_integrity(self):
        row = self.add_picture()
        self.store.update([row['id']], {'project': '备份项目'})
        path = self.store.backup()
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT project FROM invoices').fetchone()[0], '备份项目')

    def test_server_port_is_exclusive(self):
        server = LocalServer(('127.0.0.1', 0), Handler)
        try:
            with self.assertRaises(OSError):
                duplicate = LocalServer(('127.0.0.1', server.server_port), Handler)
                duplicate.server_close()
        finally:
            server.server_close()

    def test_http_export_preview_and_csrf(self):
        from reportlab.pdfgen import canvas
        sample = self.root / 'sample.pdf'
        page = canvas.Canvas(str(sample))
        page.drawString(60, 750, 'SYNTHETIC INVOICE - QA ONLY')
        page.save()
        self.store.scan()
        row = self.store.state()['invoices'][0]
        self.store.update([row['id']], {'notes': '=HYPERLINK("test")', 'amount': '4.00', 'invoice_no': '12345678901234567890'})
        row = self.store.state()['invoices'][0]
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.store = self.store
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with self.opener.open(url + '/api/state') as response:
                self.assertEqual(len(json.load(response)['invoices']), 1)
            with self.opener.open(url + '/api/export') as response:
                csv = response.read().decode('utf-8-sig')
                self.assertIn("'=HYPERLINK", csv)
                self.assertIn("'" + row['invoice_no'], csv)
                self.assertIn('4.00', csv)
            with self.opener.open(url + '/api/preview/' + row['id']) as response:
                self.assertEqual(response.read(8), b'\x89PNG\r\n\x1a\n')
            with self.opener.open(url + '/api/file/' + row['id']) as response:
                self.assertEqual(response.read(4), b'%PDF')
            request = urllib.request.Request(url + '/api/update', data=b'{}', method='POST')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.opener.open(request)
            self.assertEqual(caught.exception.code, 403)
            request = urllib.request.Request(url + '/api/scan', data=b'{}', headers={'X-Workbench-Key': self.store.key, 'Origin': 'https://example.com'}, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.opener.open(request)
            self.assertEqual(caught.exception.code, 403)
            request = urllib.request.Request(url + '/api/update', data=json.dumps({'ids': [row['id']], 'patch': {'status': '报销中'}}).encode(), headers={'X-Workbench-Key': self.store.key, 'Content-Type': 'application/json'}, method='POST')
            with self.opener.open(request) as response:
                self.assertTrue(json.load(response)['ok'])
            self.assertEqual(self.store.state()['invoices'][0]['status'], '报销中')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
