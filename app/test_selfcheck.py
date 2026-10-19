"""自检发现的启动、恢复及异常文件边界回归；均使用隔离目录。"""
import unittest
from unittest.mock import patch

import server
import test_receipt_jobs as fixtures


class StartupTests(unittest.TestCase):
    def test_busy_port_does_not_open_or_modify_the_database(self):
        with patch('sys.argv', ['server.py', '--no-browser']), \
             patch('server.LocalServer', side_effect=OSError('occupied')), \
             patch('server.Store') as store, \
             patch('urllib.request.OpenerDirector.open', side_effect=OSError('not a workbench')):
            with self.assertRaises(SystemExit):
                server.main()
            store.assert_not_called()

    def test_failed_initial_scan_closes_database_and_reserved_port(self):
        with patch('sys.argv', ['server.py', '--no-browser']), \
             patch('server.LocalServer') as bound, \
             patch('server.Store') as store:
            store.return_value.scan.side_effect = OSError('unreadable folder')
            with self.assertRaises(OSError):
                server.main()
            bound.return_value.server_close.assert_called_once()
            store.return_value.db.close.assert_called_once()


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.ReceiptJobTests.setUp
    tearDown = fixtures.ReceiptJobTests.tearDown
    fake_script = fixtures.ReceiptJobTests.fake_script
    def test_restart_keeps_workbench_usable_when_old_file_is_locked(self):
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script), patch.object(self.store.receipts, 'prune'):
            for _ in range(6):
                self.store.receipts.create([self.row['id']], background=False)
        self.store.db.close()
        with patch('receipt_jobs.shutil.rmtree', side_effect=PermissionError('Word占用文件')):
            self.store = server.Store(self.root, self.root / 'app' / 'data')
        state = self.store.state()
        self.assertEqual(len(state['invoices']), 1)
        self.assertTrue(state['receipt_skill']['maintenance_warning'])
        self.store.receipts.prune()
        self.assertEqual(len(self.store.receipts.documents()), 5)
        self.assertFalse(self.store.receipts.availability().get('maintenance_warning'))

    def test_restart_keeps_other_features_when_layout_update_fails(self):
        with patch('receipt_jobs.subprocess.run', side_effect=self.fake_script):
            job = self.store.receipts.create([self.row['id']], background=False)
        job['layout_version'] = 0
        self.store.receipts._save(job)
        self.store.db.close()
        with patch('receipt_jobs.clean_receipt', side_effect=PermissionError('Word占用文件')):
            self.store = server.Store(self.root, self.root / 'app' / 'data')
        self.assertTrue(self.store.state()['receipt_skill']['maintenance_warning'])
        self.assertEqual(len(self.store.documents()), 1)
        self.assertEqual(self.store.receipts.get(job['id'])['layout_version'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
