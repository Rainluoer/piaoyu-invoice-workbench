"""类型修改、完整历史、原文件保留和事务回滚。"""
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import urllib.request
from contextlib import closing

from server import Store, LocalServer, Handler


class CategoryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.original = self.root / 'invoice.png'
        self.original.write_bytes(b'original invoice')
        self.store = Store(self.root, self.root / 'app' / 'data')
        self.store.scan()
        self.row = self.store.state()['invoices'][0]

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_type_and_before_after_history_persist_after_rescan_restart_and_backup(self):
        self.store.update([self.row['id']], {'category': '科研材料'})
        self.store.update([self.row['id']], {'category': '出行交通'})
        self.store.scan()
        self.store.db.close()
        self.store = Store(self.root, self.root / 'app' / 'data')
        saved = self.store.state()['invoices'][0]
        self.assertEqual(saved['category'], '出行交通')
        self.assertEqual(saved['status'], self.row['status'])
        history = self.store.category_history(self.row['id'])
        self.assertEqual([(item['before'], item['after']) for item in history], [('科研材料', '出行交通'), ('其他票据', '科研材料')])
        self.assertTrue(all(item['created_at'] for item in history))
        self.assertEqual(self.original.read_bytes(), b'original invoice')
        backup = self.store.backup()
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE action='修改发票类型'").fetchone()[0], 2)

    def test_same_value_does_not_duplicate_history_and_invalid_type_is_rejected(self):
        self.store.update([self.row['id'], self.row['id']], {'category': '科研材料'})
        self.store.update([self.row['id']], {'category': ' 科研材料 '})
        self.assertEqual(len(self.store.category_history(self.row['id'])), 1)
        for value in ['', '   ', '字' * 81, '实验耗材', '全部分类', None, 1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.update([self.row['id']], {'category': value})
        self.assertEqual(self.store.state()['invoices'][0]['category'], '科研材料')
        self.assertEqual(len(self.store.category_history(self.row['id'])), 1)

    def test_failed_audit_insert_rolls_back_type_and_no_partial_batch(self):
        with patch.object(self.store, 'event', side_effect=sqlite3.OperationalError('write failed')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.update([self.row['id']], {'category': '科研材料'})
        self.assertEqual(self.store.state()['invoices'][0]['category'], self.row['category'])
        self.assertEqual(self.store.category_history(self.row['id']), [])
        with self.assertRaises(ValueError):
            self.store.update([self.row['id'], 'missing'], {'category': '科研材料'})
        self.assertEqual(self.store.category_history(self.row['id']), [])

    def test_complete_history_is_not_limited_to_recent_activity_feed(self):
        for index in range(35):
            self.store.update([self.row['id']], {'category': ['科研材料', '出行交通'][index % 2]})
        self.assertEqual(len(self.store.state()['events']), 30)
        history = self.store.category_history(self.row['id'])
        self.assertEqual(len(history), 35)
        self.assertEqual(history[0]['after'], '科研材料')
        self.assertEqual(history[-1]['before'], self.row['category'])

    def test_http_history_reflects_standard_detail_update(self):
        server = LocalServer(('127.0.0.1', 0), Handler)
        server.store = self.store
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            request = urllib.request.Request(base + '/api/update', data=json.dumps({'ids':[self.row['id']], 'patch':{'category':'科研材料','notes':'核对后调整'}}).encode(), headers={'X-Workbench-Key':self.store.key,'Content-Type':'application/json'})
            with opener.open(request) as response:
                self.assertTrue(json.load(response)['ok'])
            with opener.open(base + '/api/category-history/' + self.row['id']) as response:
                history = json.load(response)
            self.assertEqual(history[0]['before'], self.row['category'])
            self.assertEqual(history[0]['after'], '科研材料')
            with opener.open(base + '/categories.js') as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
