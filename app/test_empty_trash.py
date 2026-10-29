"""永久清空仅处理确认时的回收站快照；失败可恢复并安全重试。"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from zipfile import ZipFile

from server import Handler, LocalServer, Store


class EmptyTrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root, self.root / 'app/data')
        self.files = {name: self.root / (name + '.png') for name in ('first', 'second', 'active')}
        for name, path in self.files.items():
            path.write_bytes(('synthetic ' + name).encode())
        self.store.scan()
        self.ids = {Path(row['path']).stem: row['id'] for row in self.store.state()['invoices']}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def row(self, name):
        return dict(self.store.db.execute('SELECT * FROM invoices WHERE id=?', (self.ids[name],)).fetchone())

    def recycle(self, *names):
        self.store.delete_invoices([self.ids[name] for name in names])
        return {name: Path(self.row(name)['trash_path']) if self.row(name)['trash_path'] else None for name in names}

    def assert_failed(self, result, name, remaining=1):
        self.assertEqual(result['purged'], 0)
        self.assertEqual([item['id'] for item in result['failed']], [self.ids[name]])
        self.assertTrue(result['failed'][0]['error'])
        self.assertEqual(result['remaining'], remaining)

    def test_requires_exact_confirmation_and_empty_snapshot_is_idempotent(self):
        paths = self.recycle('first')
        for confirmed in (None, False, 1, 'true'):
            with self.subTest(confirmed=confirmed), self.assertRaises(ValueError):
                self.store.empty_trash([self.ids['first']], confirmed=confirmed)
            self.assertTrue(paths['first'].is_file())
            self.assertFalse(self.row('first')['purged_at'])
        with self.assertRaises(ValueError):
            self.store.empty_trash([self.ids['first']])
        self.assertEqual(self.store.empty_trash([], confirmed=True), {'purged': 0, 'failed': [], 'remaining': 1})

    def test_success_preserves_active_files_receipts_and_scan_tombstones(self):
        paths = self.recycle('first', 'second')
        active_before = self.row('active')
        receipt = self.store.receipts.directory / 'keep' / 'receipt.docx'
        receipt.parent.mkdir(parents=True)
        receipt.write_bytes(b'unrelated generated document')
        ids = [self.ids['first'], self.ids['second']]
        result = self.store.empty_trash(ids, confirmed=True)
        self.assertEqual(result, {'purged': 2, 'failed': [], 'remaining': 0})
        self.assertTrue(all(not path.exists() for path in paths.values()))
        self.assertEqual(self.row('active'), active_before)
        self.assertEqual(self.files['active'].read_bytes(), b'synthetic active')
        self.assertEqual(receipt.read_bytes(), b'unrelated generated document')
        for name in ('first', 'second'):
            self.assertTrue(self.row(name)['deleted_at'])
            self.assertTrue(self.row(name)['purged_at'])
        self.assertEqual(self.store.state()['trash'], [])
        with self.assertRaises(ValueError):
            self.store.restore_invoices([self.ids['first']])
        # 同一路径再次出现时，保留永久清理的墓碑，不重新导入旧记录。
        self.files['first'].write_bytes(b'synthetic first')
        self.store.scan()
        self.assertEqual({row['id'] for row in self.store.state()['invoices']}, {self.ids['active']})
        self.assertTrue(self.files['first'].is_file())
        self.assertEqual(self.store.empty_trash(ids, confirmed=True), {'purged': 0, 'failed': [], 'remaining': 0})
        self.store.db.close()
        self.store = Store(self.root, self.root / 'app/data')
        self.store.scan()
        self.assertEqual(self.store.state()['trash'], [])
        self.assertNotIn(self.ids['first'], {row['id'] for row in self.store.state()['invoices']})

    def test_snapshot_does_not_clear_later_recycled_entries(self):
        paths = self.recycle('first')
        snapshot = [row['id'] for row in self.store.state()['trash']]
        later = self.recycle('second')['second']
        self.assertEqual(self.store.empty_trash(snapshot, confirmed=True), {'purged': 1, 'failed': [], 'remaining': 1})
        self.assertFalse(paths['first'].exists())
        self.assertTrue(later.is_file())
        self.assertEqual([row['id'] for row in self.store.state()['trash']], [self.ids['second']])
        self.store.restore_invoices([self.ids['second']])
        self.assertEqual(self.files['second'].read_bytes(), b'synthetic second')

    def test_active_unknown_and_restored_ids_reject_entire_snapshot(self):
        paths = self.recycle('first', 'second')
        for invalid in (self.ids['active'], 'unknown'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.empty_trash([self.ids['first'], invalid], confirmed=True)
            self.assertTrue(paths['first'].is_file())
            self.assertFalse(self.row('first')['purged_at'])
        self.store.restore_invoices([self.ids['second']])
        with self.assertRaises(ValueError):
            self.store.empty_trash([self.ids['first'], self.ids['second']], confirmed=True)
        self.assertTrue(paths['first'].is_file())
        self.assertEqual(self.files['second'].read_bytes(), b'synthetic second')

    def test_outside_trash_path_rejects_before_any_file_changes(self):
        paths = self.recycle('first', 'second')
        outside = self.root / 'must-keep.bin'
        outside.write_bytes(b'outside trash')
        self.store.db.execute('UPDATE invoices SET trash_path=? WHERE id=?', (str(outside), self.ids['second']))
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.store.empty_trash([self.ids['first'], self.ids['second']], confirmed=True)
        self.assertTrue(paths['first'].is_file())
        self.assertTrue(paths['second'].is_file())
        self.assertFalse(self.row('first')['purged_at'])
        self.assertEqual(outside.read_bytes(), b'outside trash')

    def test_linked_trash_file_rejects_before_any_file_changes(self):
        paths = self.recycle('first', 'second')
        linked = paths['second'].with_name('linked-second.png')
        try:
            linked.symlink_to(paths['second'])
        except OSError as error:
            self.skipTest(f'当前系统无法创建符号链接：{error}')
        self.store.db.execute('UPDATE invoices SET trash_path=? WHERE id=?', (str(linked), self.ids['second']))
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.store.empty_trash([self.ids['first'], self.ids['second']], confirmed=True)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(paths['first'].read_bytes(), b'synthetic first')
        self.assertEqual(paths['second'].read_bytes(), b'synthetic second')
        self.assertFalse(self.row('first')['purged_at'])
        self.assertFalse(self.row('second')['purged_at'])

    def test_zip_rescan_does_not_recreate_permanently_removed_import(self):
        archive = self.root / 'copy.zip'
        with ZipFile(archive, 'w') as output:
            output.writestr('copy.png', b'synthetic first')
        archive_before = archive.read_bytes()
        self.store.scan()
        imported = next(row for row in self.store.state()['invoices'] if row['source'].endswith('copy.zip'))
        imported_path = Path(imported['path'])
        self.assertTrue(imported_path.is_relative_to(self.store.data_dir / 'imports'))
        self.store.delete_invoices([imported['id']])
        self.assertEqual(self.store.empty_trash([imported['id']], confirmed=True), {'purged': 1, 'failed': [], 'remaining': 0})
        for reopened in (False, True):
            if reopened:
                self.store.db.close()
                self.store = Store(self.root, self.root / 'app/data')
            with self.subTest(reopened=reopened):
                self.store.scan()
                self.assertFalse(imported_path.exists())
                self.assertEqual({row['id'] for row in self.store.state()['invoices']}, set(self.ids.values()))
                self.assertEqual(self.store.state()['trash'], [])
                tombstone = self.store.db.execute('SELECT deleted_at,purged_at FROM invoices WHERE id=?', (imported['id'],)).fetchone()
                self.assertTrue(tombstone['deleted_at'])
                self.assertTrue(tombstone['purged_at'])
                self.assertEqual(archive.read_bytes(), archive_before)
                self.assertEqual(self.files['first'].read_bytes(), b'synthetic first')

    def test_missing_original_record_can_be_permanently_removed_without_file(self):
        self.files['first'].unlink()
        self.recycle('first')
        self.assertEqual(self.row('first')['trash_path'], '')
        self.assertEqual(self.store.empty_trash([self.ids['first']], confirmed=True), {'purged': 1, 'failed': [], 'remaining': 0})
        self.assertTrue(self.row('first')['deleted_at'])
        self.assertTrue(self.row('first')['purged_at'])
        self.assertEqual(self.store.state()['trash'], [])
        self.assertEqual(self.files['active'].read_bytes(), b'synthetic active')
        missing_trash = self.recycle('second')['second']
        missing_trash.unlink()
        self.assertEqual(self.store.empty_trash([self.ids['second']], confirmed=True), {'purged': 1, 'failed': [], 'remaining': 0})
        self.assertTrue(self.row('second')['purged_at'])

    def test_large_snapshot_does_not_use_the_normal_batch_limit(self):
        self.recycle('first')
        template = self.row('first')
        columns = list(template)
        records = []
        snapshot = [self.ids['first']]
        # 既有永久清理墓碑仍可出现在重试快照中；不创建/扫描两千个文件。
        for index in range(2000):
            row = {**template, 'id': f'old-purged-{index}', 'path': str(self.root / f'missing-{index}.png'),
                   'filename': f'missing-{index}.png', 'trash_path': '', 'purged_at': template['deleted_at']}
            snapshot.append(row['id'])
            records.append(tuple(row[column] for column in columns))
        self.store.db.executemany(f"INSERT INTO invoices({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", records)
        self.store.db.commit()
        self.assertEqual(len(snapshot), 2001)
        self.assertEqual(self.store.empty_trash(snapshot, confirmed=True), {'purged': 1, 'failed': [], 'remaining': 0})

    def test_legacy_database_migration_preserves_recoverable_trash(self):
        self.store.update([self.ids['first']], {'notes': 'legacy trash note'})
        path = self.recycle('first')['first']
        before = self.row('first')
        database = self.store.db_path
        self.store.db.close()
        with closing(sqlite3.connect(database)) as db:
            db.execute('ALTER TABLE invoices DROP COLUMN purged_at')
            db.commit()
        self.store = Store(self.root, self.root / 'app/data')
        after = self.row('first')
        self.assertEqual(after['purged_at'], '')
        self.assertEqual([after[field] for field in ('deleted_at', 'trash_path', 'notes')],
                         [before[field] for field in ('deleted_at', 'trash_path', 'notes')])
        self.assertTrue(path.is_file())
        self.assertEqual([row['id'] for row in self.store.state()['trash']], [self.ids['first']])
        self.assertEqual(self.store.restore_invoices([self.ids['first']])['restored'], 1)
        self.assertEqual(self.files['first'].read_bytes(), b'synthetic first')

    def test_audit_failure_restores_file_and_state_then_can_retry(self):
        path = self.recycle('first')['first']
        before = self.row('first')
        with patch.object(self.store, 'event', side_effect=sqlite3.OperationalError('synthetic audit failure')):
            result = self.store.empty_trash([self.ids['first']], confirmed=True)
        self.assert_failed(result, 'first')
        self.assertFalse(self.store.db.in_transaction)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b'synthetic first')
        self.assertFalse(self.row('first')['purged_at'])
        self.assertEqual(self.row('first')['deleted_at'], before['deleted_at'])
        self.assertEqual(self.store.empty_trash([self.ids['first']], confirmed=True)['purged'], 1)

    def test_later_database_failure_preserves_an_earlier_success(self):
        paths = self.recycle('first', 'second')
        original_event = self.store.event

        def fail_second(id_, *args, **kwargs):
            if id_ == self.ids['second']:
                raise sqlite3.OperationalError('synthetic second audit failure')
            return original_event(id_, *args, **kwargs)

        with patch.object(self.store, 'event', fail_second):
            result = self.store.empty_trash([self.ids['first'], self.ids['second']], confirmed=True)
        self.assertEqual(result['purged'], 1)
        self.assertEqual([row['id'] for row in result['failed']], [self.ids['second']])
        self.assertEqual(result['remaining'], 1)
        self.assertFalse(paths['first'].exists())
        self.assertTrue(self.row('first')['purged_at'])
        self.assertEqual(paths['second'].read_bytes(), b'synthetic second')
        self.assertFalse(self.row('second')['purged_at'])
        self.assertEqual(self.store.empty_trash([self.ids['second']], confirmed=True)['purged'], 1)

    def test_unlink_failure_is_partial_restores_file_and_can_retry(self):
        paths = self.recycle('first', 'second')
        original_unlink = Path.unlink

        def fail_first_staged(path, *args, **kwargs):
            if path.parent == paths['first'].parent and path.name.startswith('.purge-'):
                raise PermissionError('synthetic occupied file')
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', fail_first_staged):
            result = self.store.empty_trash([self.ids['first'], self.ids['second']], confirmed=True)
        self.assertEqual(result['purged'], 1)
        self.assertEqual([item['id'] for item in result['failed']], [self.ids['first']])
        self.assertEqual(result['remaining'], 1)
        self.assertTrue(paths['first'].is_file())
        self.assertEqual(paths['first'].read_bytes(), b'synthetic first')
        self.assertFalse(paths['second'].exists())
        self.assertFalse(self.row('first')['purged_at'])
        self.assertTrue(self.row('second')['purged_at'])
        self.assertEqual(list(paths['first'].parent.glob('.purge-*')), [])
        self.assertEqual(self.store.empty_trash([self.ids['first']], confirmed=True), {'purged': 1, 'failed': [], 'remaining': 0})

    def test_http_confirmation_key_and_origin_are_required(self):
        path = self.recycle('first')['first']
        server = LocalServer(('127.0.0.1', 0), Handler)
        server.store = self.store
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f'http://127.0.0.1:{server.server_port}/api/empty-trash'

        def post(body, key=True, origin=None):
            headers = {'Content-Type': 'application/json', 'X-Workbench-Key': self.store.key if key else ''}
            if origin is not None:
                headers['Origin'] = origin
            return opener.open(urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers))

        try:
            body = {'ids': [self.ids['first']], 'confirmed': True}
            with self.assertRaises(urllib.error.HTTPError) as error:
                post(body, key=False)
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as error:
                post({'ids': body['ids'], 'confirmed': False})
            self.assertEqual(error.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as error:
                post(body, origin='https://untrusted.example')
            self.assertEqual(error.exception.code, 403)
            self.assertTrue(path.is_file())
            with post(body) as response:
                self.assertEqual(json.load(response), {'purged': 1, 'failed': [], 'remaining': 0})
            self.assertFalse(path.exists())
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
