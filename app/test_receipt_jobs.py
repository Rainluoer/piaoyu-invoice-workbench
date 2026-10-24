"""生成任务的调度、输入边界和失败处理；真实 ruku 在浏览器验收中执行。"""
import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

from server import Store, Handler, LocalServer


class ReceiptJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root, self.root / 'app' / 'data')
        folder = self.root / '工具'
        folder.mkdir()
        self.original = folder / 'original.pdf'
        self.original.write_bytes(b'%PDF fixture for scheduler')
        self.store.scan()
        self.row = self.store.state()['invoices'][0]

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def fake_script(self, command, **kwargs):
        script = Path(command[1]).name
        args = dict(zip(command[2::2], command[3::2]))
        if script == 'extract_invoices.py':
            output = Path(args['--output'])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps([dict(invoice_no='12345678901234567890', total=12.34, original_filename='001.pdf', items=[dict(category='材料', product='零件', quantity=1, unit_price=10, amount=10, tax=2.34)])]), encoding='utf-8')
        if script == 'fill_receipt.py':
            Path(args['--output']).write_bytes(b'test output stub')
        return SimpleNamespace(returncode=0, stdout='Chinese: 壹拾贰元叁角肆分\n', stderr='')

    def test_three_original_scripts_in_order_and_source_unchanged(self):
        before = self.original.read_bytes()
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script) as runner:
            job = self.store.receipts.create([self.row['id']], background=False)
        self.assertEqual(job['status'], 'done')
        self.assertEqual([Path(call.args[0][1]).name for call in runner.call_args_list], ['extract_invoices.py', 'rename_files.py', 'fill_receipt.py'])
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs['env']['PIP_NO_INDEX'], '1')
        self.assertIn('--template', runner.call_args_list[2].args[0])
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(self.store.state()['invoices'][0]['path'], str(self.original))
        self.assertEqual(job['item_count'], 1)
        self.assertEqual(job['total_cents'], 1234)
        self.assertEqual(Path(job['output']).name, '入库单_已填写.docx')
        self.assertEqual(Path(job['output']).parent.name, '发票')
        self.assertEqual(self.store.documents()[0]['id'], job['document_id'])
        self.assertTrue(all(step['status'] == 'done' for step in job['steps']))

    def test_documents_only_list_registered_outputs_without_scanning_folders(self):
        external = [self.root / '手工入库单.docx',
                    self.root / '.qa-temp' / 'receipt-1.docx',
                    self.root / '工具' / '统计表格.docx',
                    self.store.receipts.directory / 'unregistered.docx']
        for path in external:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'external document')
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script):
            job = self.store.receipts.create([self.row['id']], background=False)
        with patch.object(self.store, 'source_files', side_effect=AssertionError('must not scan')) as scan:
            documents = self.store.state()['documents']
            scan.assert_not_called()
        self.assertEqual([doc['id'] for doc in documents], [job['document_id']])
        self.assertTrue(documents[0]['generated'])
        self.assertTrue(all(path.read_bytes() == b'external document' for path in external))
        Path(job['output']).unlink()
        self.assertEqual(self.store.documents(), [])

    def test_document_download_only_serves_generated_receipts(self):
        external = self.root / '手工入库单.docx'
        external.write_bytes(b'external document')
        external_id = hashlib.sha256(str(external).encode()).hexdigest()[:24]
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script):
            job = self.store.receipts.create([self.row['id']], background=False)
        server = LocalServer(('127.0.0.1', 0), Handler)
        server.store = self.store
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/document/'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(base + job['document_id']) as response:
                self.assertEqual(response.read(), Path(job['output']).read_bytes())
                self.assertIn('wordprocessingml.document', response.headers['Content-Type'])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(base + external_id)
            self.assertEqual(caught.exception.code, 404)
            self.assertEqual(external.read_bytes(), b'external document')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_response_read_failure_keeps_running_workers_exclusive_lock(self):
        entered = threading.Event()
        proceed = threading.Event()
        jobs = self.store.receipts
        def blocked_script(command, **kwargs):
            if Path(command[1]).name == 'extract_invoices.py':
                entered.set()
                if not proceed.wait(timeout=5):
                    raise RuntimeError('test worker timed out')
            return self.fake_script(command, **kwargs)
        with patch('receipt_jobs.subprocess.run', side_effect=blocked_script):
            try:
                with patch.object(jobs, 'get', side_effect=RuntimeError('response read failed')):
                    with self.assertRaisesRegex(RuntimeError, 'response read failed'):
                        jobs.create([self.row['id']])
                self.assertTrue(entered.wait(timeout=3))
                self.assertTrue(jobs.active.locked())
                with self.assertRaisesRegex(ValueError, '已有入库单正在生成'):
                    jobs.create([self.row['id']])
            finally:
                proceed.set()
                if jobs.thread:
                    jobs.thread.join(timeout=5)
        self.assertFalse(jobs.thread.is_alive())
        self.assertFalse(jobs.active.locked())
        self.assertEqual(jobs.recent()[0]['status'], 'done')

    def test_incomplete_extraction_does_not_generate_empty_receipt(self):
        def incomplete(command, **kwargs):
            result = self.fake_script(command, **kwargs)
            output = Path(command[command.index('--output') + 1])
            output.write_text('[{"invoice_no":"123","total":12.34}]', encoding='utf-8')
            return result

        with patch('receipt_jobs.subprocess.run', side_effect=incomplete) as runner:
            job = self.store.receipts.create([self.row['id']], background=False)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(runner.call_count, 1)
        self.assertFalse(Path(job['output']).exists())
        self.assertEqual(self.store.documents(), [])

    def test_failed_rename_does_not_run_fill(self):
        def fail_rename(command, **kwargs):
            if Path(command[1]).name == 'rename_files.py':
                return SimpleNamespace(returncode=1, stdout='', stderr='rename failed')
            return self.fake_script(command, **kwargs)

        with patch('receipt_jobs.subprocess.run', side_effect=fail_rename) as runner:
            job = self.store.receipts.create([self.row['id']], background=False)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['steps'][1]['status'], 'failed')
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(self.store.documents(), [])

    def test_changed_source_stops_before_script(self):
        self.original.write_bytes(b'new PDF content')
        with patch('receipt_jobs.subprocess.run') as runner:
            job = self.store.receipts.create([self.row['id']], background=False)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('文件已变化', job['error'])
        runner.assert_not_called()

    def test_duplicate_or_nonmaterial_cannot_be_selected(self):
        copy = self.original.with_name('copy.pdf')
        copy.write_bytes(self.original.read_bytes())
        self.store.scan()
        duplicate = next(row for row in self.store.state()['invoices'] if row['duplicate_of'])
        with self.assertRaises(ValueError):
            self.store.receipts.create([duplicate['id']], background=False)
        original = next(row for row in self.store.state()['invoices'] if not row['duplicate_of'])
        self.store.update([original['id']], {'category': '铁路客票'})
        with self.assertRaises(ValueError):
            self.store.receipts.create([original['id']], background=False)

    def test_previous_output_retained_and_restart_marks_interrupted_job(self):
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script):
            first = self.store.receipts.create([self.row['id']], background=False)
            second = self.store.receipts.create([self.row['id']], background=False)
        self.assertNotEqual(first['output'], second['output'])
        self.assertTrue(Path(first['output']).exists())
        self.assertEqual(len(self.store.documents()), 2)
        with patch('receipt_jobs.threading.Thread.start'):
            interrupted = self.store.receipts.create([self.row['id']])
        with self.assertRaises(ValueError):
            self.store.receipts.create([self.row['id']])
        self.store.receipts.active.release()
        self.store.db.close()
        self.store = Store(self.root, self.root / 'app' / 'data')
        self.assertEqual(self.store.receipts.get(interrupted['id'])['status'], 'failed')

    def test_only_five_latest_outputs_and_task_directories_are_retained(self):
        before = self.original.read_bytes()
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script):
            jobs = [self.store.receipts.create([self.row['id']], background=False) for _ in range(7)]
        self.assertTrue(all(job['status'] == 'done' for job in jobs))
        self.assertEqual(len(self.store.documents()), 5)
        self.assertEqual({j['id'] for j in self.store.receipts.recent()}, {j['id'] for j in jobs[-5:]})
        for job in jobs[:2]:
            self.assertFalse(Path(job['output']).parent.parent.exists())
            with self.assertRaises(ValueError):
                self.store.receipts.get(job['id'])
        self.assertTrue(all(Path(j['output']).is_file() for j in jobs[-5:]))
        self.assertEqual(before, self.original.read_bytes())

    def test_restart_enforces_limit_and_invalid_path_is_never_deleted(self):
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script), patch.object(self.store.receipts, 'prune'):
            jobs = [self.store.receipts.create([self.row['id']], background=False) for _ in range(6)]
        jobs[0]['output'] = str(self.original)
        self.store.receipts._save(jobs[0])
        with self.assertRaises(ValueError):
            self.store.receipts.prune()
        self.assertTrue(self.original.is_file())
        jobs[0]['output'] = str(self.store.receipts.directory / jobs[0]['id'] / '发票' / '入库单_已填写.docx')
        self.store.receipts._save(jobs[0])
        self.store.db.close()
        self.store = Store(self.root, self.root / 'app' / 'data')
        self.assertEqual(len(self.store.documents()), 5)
        self.assertFalse(Path(jobs[0]['output']).exists())

    def test_cleanup_failure_is_visible_and_retry_removes_the_old_output(self):
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script):
            for _ in range(5):
                self.store.receipts.create([self.row['id']], background=False)
            with patch('receipt_jobs.shutil.rmtree', side_effect=PermissionError('文件被占用')):
                job = self.store.receipts.create([self.row['id']], background=False)
        self.assertEqual(job['status'], 'done')
        self.assertIn('未能清理', job['retention_error'])
        self.store.receipts.prune()
        self.assertEqual(len(self.store.documents()), 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
