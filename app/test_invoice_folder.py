"""切换扫描目录的持久化、历史台账兼容与接口边界。"""
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from server import BASE, Store, Handler, LocalServer
from folder_picker import pick_folder


class InvoiceFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.old = self.parent / '原目录'
        self.new = self.parent / '新目录 空格'
        self.old.mkdir(); self.new.mkdir()
        self.store = Store(self.old, self.parent / 'app' / 'data')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_switch_persists_scans_subfolders_and_preserves_history(self):
        original = self.old / '原票.png'
        original.write_bytes(b'old invoice')
        (self.old / '原有入库单.docx').write_bytes(b'old document')
        self.store.scan()
        row = self.store.state()['invoices'][0]
        self.store.update([row['id']], {'project': '原项目', 'notes': '原备注', 'status': '已报销'})
        (self.new / '子目录').mkdir()
        (self.new / '子目录' / '新票.png').write_bytes(b'new invoice')
        result = self.store.set_invoice_root(str(self.new))
        self.assertEqual(result['scan']['added'], 1)
        self.assertEqual(len(self.store.state()['invoices']), 2)
        self.assertEqual(self.store.invoice_path(original), original)
        self.assertEqual(self.store.documents(), [])
        self.assertEqual((self.old / '原有入库单.docx').read_bytes(), b'old document')
        self.store.db.close()
        self.store = Store(self.old, self.parent / 'app' / 'data')
        self.assertEqual(self.store.root, self.new)
        saved = next(r for r in self.store.state()['invoices'] if r['id'] == row['id'])
        self.assertEqual((saved['project'], saved['notes'], saved['status']), ('原项目', '原备注', '已报销'))
        self.assertEqual(original.read_bytes(), b'old invoice')
        self.assertEqual(self.store.documents(), [])
        self.assertEqual((self.old / '原有入库单.docx').read_bytes(), b'old document')

    def test_invalid_paths_and_running_generation_do_not_change_settings(self):
        file = self.new / 'not-folder.txt'
        file.write_text('test')
        for value in ['', 'relative/path', str(file), str(self.new / 'missing'), str(BASE), str(self.store.data_dir)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.set_invoice_root(value)
            self.assertEqual(self.store.root, self.old)
        self.store.receipts.active.acquire()
        try:
            with self.assertRaisesRegex(ValueError, '正在生成'):
                self.store.set_invoice_root(str(self.new))
        finally:
            self.store.receipts.active.release()
        self.assertEqual(self.store.root, self.old)

    def test_selected_folder_name_contributes_status_and_category(self):
        folder = self.new / '已报销' / '工具'
        folder.mkdir(parents=True)
        (folder / '材料.png').write_bytes(b'material invoice')
        self.store.set_invoice_root(str(folder))
        row = self.store.state()['invoices'][0]
        self.assertEqual(row['status'], '已报销')
        self.assertEqual(row['category'], '科研材料')

    def test_history_files_can_still_be_deleted_and_restored_after_switch(self):
        (self.old / 'a.png').write_bytes(b'identical invoice')
        (self.old / 'b.png').write_bytes(b'identical invoice')
        self.store.scan()
        self.store.set_invoice_root(str(self.new))
        duplicate = next(r for r in self.store.state()['invoices'] if r['duplicate_of'])
        self.assertEqual(self.store.delete_duplicates([duplicate['id']])['deleted'], 1)
        self.assertEqual(self.store.restore_duplicates([duplicate['id']])['restored'], 1)
        self.assertTrue(Path(duplicate['path']).is_file())

    def test_http_settings_and_picker_keep_authentication_and_cancel_is_read_only(self):
        server = LocalServer(('127.0.0.1', 0), Handler)
        server.store = self.store
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(route, body, key=True):
            headers = {'Content-Type': 'application/json'}
            if key: headers['X-Workbench-Key'] = self.store.key
            return opener.open(urllib.request.Request(base + route, data=json.dumps(body).encode(), headers=headers))
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                post('/api/settings/invoice-folder', {'path': str(self.new)}, key=False)
            self.assertEqual(caught.exception.code, 403)
            with patch('server.pick_folder', return_value={'cancelled': True, 'path': ''}):
                with post('/api/pick-folder', {}) as response:
                    self.assertTrue(json.load(response)['cancelled'])
            self.assertEqual(self.store.root, self.old)
            with post('/api/settings/invoice-folder', {'path': str(self.new)}) as response:
                self.assertEqual(json.load(response)['root'], str(self.new))
            with opener.open(base + '/folders.js') as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows folder picker')
    def test_native_picker_passes_unicode_path_without_shell_interpolation(self):
        selected = str(self.new / "测试'`$文件夹")
        encoded = base64.b64encode(selected.encode()).decode()
        with patch('folder_picker.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=encoded)) as run:
            self.assertEqual(pick_folder(self.old), {'cancelled': False, 'path': selected})
        command = run.call_args.args[0]
        script = base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn('-STA', command)
        self.assertIn('ShowNewFolderButton = $false', script)
        self.assertNotIn(str(self.old), script)
        self.assertIn(base64.b64encode(str(self.old).encode()).decode(), script)


if __name__ == '__main__':
    unittest.main(verbosity=2)
