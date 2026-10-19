"""重复副本删除和恢复的隔离回归测试。"""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from server import Handler, LocalServer, Store


class DuplicateTrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'app' / 'data'
        self.store = Store(self.root, self.data)
        self.store.scan()

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def pair(self, prefix='a', content=b'picture-a'):
        (self.root / (prefix + '-1.png')).write_bytes(content)
        (self.root / (prefix + '-2.png')).write_bytes(content)
        self.store.scan()
        rows = self.store.state()['invoices']
        duplicate = next(r for r in rows if r['duplicate_of'] and r['sha256'] == hashlib.sha256(content).hexdigest())
        original = next(r for r in rows if r['id'] == duplicate['duplicate_of'])
        return original, duplicate

    def test_delete_restore_keeps_original_and_metadata(self):
        original, duplicate = self.pair()
        self.store.update([original['id']], {'status': '已报销', 'project': '原票项目'})
        self.store.update([duplicate['id']], {'status': '报销中', 'notes': '副本历史备注'})
        self.assertEqual(self.store.delete_duplicates([duplicate['id']])['deleted'], 1)
        self.assertTrue(Path(original['path']).exists())
        self.assertFalse(Path(duplicate['path']).exists())
        self.assertEqual(len(self.store.state()['invoices']), 1)
        self.assertEqual(len(self.store.state()['trash']), 1)
        self.store.scan()
        self.store.db.close()
        self.store = Store(self.root, self.data)
        self.store.scan()
        self.assertEqual(len(self.store.state()['invoices']), 1)
        self.assertEqual(self.store.restore_duplicates([duplicate['id']])['restored'], 1)
        rows = {r['id']: r for r in self.store.state()['invoices']}
        self.assertEqual(rows[original['id']]['status'], '已报销')
        self.assertEqual(rows[original['id']]['project'], '原票项目')
        self.assertEqual(rows[duplicate['id']]['status'], '报销中')
        self.assertEqual(rows[duplicate['id']]['notes'], '副本历史备注')
        self.assertEqual(Path(duplicate['path']).read_bytes(), b'picture-a')
        self.assertEqual(self.store.state()['trash'], [])

    def test_original_or_mixed_batch_cannot_be_deleted(self):
        original, duplicate = self.pair()
        for ids in ([original['id']], [duplicate['id'], original['id']], [duplicate['id'], 'unknown']):
            with self.assertRaises(ValueError):
                self.store.delete_duplicates(ids)
            self.assertTrue(Path(original['path']).exists())
            self.assertTrue(Path(duplicate['path']).exists())
            self.assertEqual(self.store.state()['trash'], [])

    def test_changed_copy_or_original_cannot_be_deleted(self):
        original, duplicate = self.pair()
        Path(duplicate['path']).write_bytes(b'new content')
        with self.assertRaises(ValueError):
            self.store.delete_duplicates([duplicate['id']])
        Path(duplicate['path']).write_bytes(b'picture-a')
        Path(original['path']).write_bytes(b'changed original')
        with self.assertRaises(ValueError):
            self.store.delete_duplicates([duplicate['id']])
        self.assertTrue(Path(duplicate['path']).exists())

    def test_restore_does_not_overwrite_existing_file(self):
        _, duplicate = self.pair()
        self.store.delete_duplicates([duplicate['id']])
        destination = Path(duplicate['path'])
        destination.write_bytes(b'another file')
        with self.assertRaises(ValueError):
            self.store.restore_duplicates([duplicate['id']])
        self.assertEqual(destination.read_bytes(), b'another file')
        self.assertEqual(len(self.store.state()['trash']), 1)
        destination.unlink()
        self.store.restore_duplicates([duplicate['id']])
        self.assertEqual(destination.read_bytes(), b'picture-a')

    def test_zip_rescan_does_not_recreate_trashed_duplicate(self):
        (self.root / 'original.png').write_bytes(b'zip picture')
        self.store.scan()
        self.store.db.execute("UPDATE invoices SET created_at='2000-01-01T00:00:00'")
        self.store.db.commit()
        archive = self.root / 'copy.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('copy.png', b'zip picture')
        self.store.scan()
        duplicate = next(r for r in self.store.state()['invoices'] if r['duplicate_of'])
        self.assertTrue(Path(duplicate['path']).is_relative_to(self.data / 'imports'))
        self.store.delete_duplicates([duplicate['id']])
        self.store.scan()
        self.assertFalse(Path(duplicate['path']).exists())
        self.assertEqual(len(self.store.state()['invoices']), 1)
        self.store.restore_duplicates([duplicate['id']])
        self.store.scan()
        self.assertEqual(len(self.store.state()['invoices']), 2)

    def test_move_failure_rolls_back_batch(self):
        _, first = self.pair('a', b'picture-a')
        _, second = self.pair('b', b'picture-b')
        real_rename = Path.rename

        def fail_second(source, target):
            if str(source) == second['path']:
                raise PermissionError('simulated file in use')
            return real_rename(source, target)

        with patch.object(Path, 'rename', fail_second):
            with self.assertRaises(PermissionError):
                self.store.delete_duplicates([first['id'], second['id']])
        self.assertTrue(Path(first['path']).exists())
        self.assertTrue(Path(second['path']).exists())
        self.assertEqual(len(self.store.state()['invoices']), 4)
        self.assertEqual(self.store.state()['trash'], [])

    def test_http_delete_and_restore(self):
        original, duplicate = self.pair()
        server = LocalServer(('127.0.0.1', 0), Handler)
        server.store = self.store
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f'http://127.0.0.1:{server.server_port}'

        def post(route, id_):
            request = urllib.request.Request(url + route, data=json.dumps({'ids': [id_]}).encode(), headers={'X-Workbench-Key': self.store.key}, method='POST')
            with opener.open(request) as response:
                return json.load(response)

        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                post('/api/delete-duplicates', original['id'])
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(post('/api/delete-duplicates', duplicate['id'])['deleted'], 1)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(url + '/api/file/' + duplicate['id'])
            self.assertEqual(caught.exception.code, 404)
            self.assertEqual(post('/api/restore-duplicates', duplicate['id'])['restored'], 1)
            with opener.open(url + '/api/file/' + duplicate['id']) as response:
                self.assertEqual(response.read(), b'picture-a')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
