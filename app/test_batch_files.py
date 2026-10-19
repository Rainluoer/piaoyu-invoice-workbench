"""普通票据批量回收、原件 ZIP 和指定目录排除。"""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from zipfile import ZipFile

from server import Store, LocalServer, Handler


class BatchFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root, self.root/'app/data')
        self.first = self.root/'甲'/'同名票据.png'
        self.second = self.root/'乙'/'同名票据.png'
        self.other = self.root/'未选中.png'
        for file,content in ((self.first,b'first invoice'),(self.second,b'second invoice'),(self.other,b'other invoice')):
            file.parent.mkdir(exist_ok=True)
            file.write_bytes(content)
        self.store.scan()
        self.rows = {Path(row['path']):row for row in self.store.state()['invoices']}
        self.ids = [self.rows[self.first]['id'],self.rows[self.second]['id']]

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_delete_originals_rescan_restart_and_restore_preserves_fields(self):
        self.store.update(self.ids,{'notes':'批量备注','status':'已报销'})
        self.assertEqual(self.store.delete_invoices(self.ids)['deleted'],2)
        self.assertFalse(self.first.exists())
        self.assertFalse(self.second.exists())
        self.assertEqual(self.other.read_bytes(),b'other invoice')
        self.store.scan()
        self.store.db.close()
        self.store=Store(self.root,self.root/'app/data')
        self.assertEqual(len(self.store.state()['invoices']),1)
        self.assertEqual(len(self.store.state()['trash']),2)
        self.store.restore_invoices(self.ids)
        restored={row['id']:row for row in self.store.state()['invoices']}
        self.assertTrue(all(restored[id_]['notes']=='批量备注' and restored[id_]['status']=='已报销' for id_ in self.ids))
        self.assertEqual(self.first.read_bytes(),b'first invoice')
        self.assertEqual(self.second.read_bytes(),b'second invoice')

    def test_delete_mixed_original_and_duplicate_is_allowed_and_atomic(self):
        duplicate=self.root/'副本.png'
        duplicate.write_bytes(self.first.read_bytes())
        self.store.scan()
        duplicate_id=next(row['id'] for row in self.store.state()['invoices'] if row['path']==str(duplicate))
        original_rename=Path.rename
        def fail_second(source,target):
            if source==duplicate:
                raise PermissionError('模拟文件占用')
            return original_rename(source,target)
        ids=[self.ids[0],duplicate_id]
        with patch.object(Path,'rename',fail_second), self.assertRaises(PermissionError):
            self.store.delete_invoices(ids)
        self.assertTrue(self.first.exists() and duplicate.exists())
        self.assertEqual(self.store.state()['trash'],[])
        self.assertEqual(self.store.delete_invoices(ids)['deleted'],2)
        self.assertTrue(self.second.exists())

    def test_unknown_or_changed_source_and_database_failure_do_not_partially_delete(self):
        for ids in ([self.ids[0],'unknown'],):
            with self.assertRaises(ValueError):self.store.delete_invoices(ids)
        self.second.write_bytes(b'changed source')
        with self.assertRaises(ValueError):self.store.delete_invoices(self.ids)
        self.assertTrue(self.first.exists())
        self.second.write_bytes(b'second invoice')
        with patch.object(self.store,'event',side_effect=RuntimeError('audit write failed')):
            with self.assertRaises(RuntimeError):self.store.delete_invoices(self.ids)
        self.assertTrue(self.first.exists() and self.second.exists())
        self.assertEqual(self.store.state()['trash'],[])

    def test_zip_contains_only_selected_originals_with_unique_safe_unicode_names(self):
        content=self.store.export_zip([self.ids[1],self.ids[0],self.ids[1]])
        with ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(len(archive.namelist()),2)
            self.assertEqual([archive.read(name) for name in archive.namelist()],[b'second invoice',b'first invoice'])
            self.assertTrue(all('同名票据.png' in name and '/' not in name and '\\' not in name for name in archive.namelist()))
        self.assertEqual(len(self.store.state()['invoices']),3)
        self.second.write_bytes(b'changed source')
        with self.assertRaises(ValueError):self.store.export_zip(self.ids)
        self.second.unlink()
        with self.assertRaises(ValueError):self.store.export_zip(self.ids)
        with patch('server.MAX_ZIP',1),self.assertRaisesRegex(ValueError,'200 MB'):
            self.store.export_zip([self.ids[0]])

    def test_missing_record_can_be_recycled_and_restore_does_not_overwrite(self):
        self.first.unlink()
        self.store.delete_invoices([self.ids[0]])
        self.store.restore_invoices([self.ids[0]])
        self.assertTrue(next(row for row in self.store.state()['invoices'] if row['id']==self.ids[0])['missing'])
        self.store.delete_invoices([self.ids[1]])
        self.second.write_bytes(b'new file')
        with self.assertRaises(ValueError):self.store.restore_invoices([self.ids[1]])
        self.assertEqual(self.second.read_bytes(),b'new file')

    def test_ignored_directory_excludes_existing_rows_zip_documents_and_metadata(self):
        ignored=self.root/'工具/入库单数据'
        ignored.mkdir(parents=True)
        preview=ignored/'预览.png'
        preview.write_bytes(b'generated preview')
        (ignored/'已有入库单.docx').write_bytes(b'document fixture')
        (ignored/'invoice_data.json').write_text(json.dumps([{'invoice_no':'SHOULD-NOT-READ','total':99}]),encoding='utf-8')
        with ZipFile(ignored/'副本.zip','w') as archive:archive.writestr('里面的票据.png',b'ignored zip invoice')
        self.store.scan()
        preview_row=next(row for row in self.store.state()['invoices'] if row['path']==str(preview))
        old_import=next(row for row in self.store.state()['invoices'] if row['source'].endswith('副本.zip'))
        imports_before=set((self.store.data_dir/'imports').rglob('*'))
        with patch('server.IGNORED_DIRECTORIES',(ignored.resolve(),)):
            self.assertNotIn('SHOULD-NOT-READ',self.store.metadata())
            self.store.scan()
            self.assertNotIn(preview_row['id'],{row['id'] for row in self.store.state()['invoices']})
            self.assertNotIn(old_import['id'],{row['id'] for row in self.store.state()['invoices']})
            self.assertFalse(any(doc['path'].startswith(str(ignored)) for doc in self.store.documents()))
            self.assertEqual(set((self.store.data_dir/'imports').rglob('*')),imports_before)
            self.assertNotIn(preview,{path for path in self.store.source_files(self.root)})
            self.assertEqual(self.store.ingest(preview,'ignored',{}),'skipped')
            with self.assertRaises(ValueError):self.store.delete_invoices([preview_row['id']])
            self.assertEqual(preview.read_bytes(),b'generated preview')
            self.assertEqual(Path(old_import['path']).read_bytes(),b'ignored zip invoice')
            self.assertEqual(self.store.state()['trash'],[])
        self.assertIn(preview_row['id'],{row['id'] for row in self.store.state()['invoices']})
        self.assertEqual(self.store.db.execute('SELECT deleted_at FROM invoices WHERE id=?',(preview_row['id'],)).fetchone()[0],'')

    def test_http_zip_delete_restore_are_authenticated(self):
        server=LocalServer(('127.0.0.1',0),Handler)
        server.store=self.store
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base=f'http://127.0.0.1:{server.server_port}'
        def post(path,key=True):
            return opener.open(urllib.request.Request(base+path,data=json.dumps({'ids':self.ids}).encode(),headers={'X-Workbench-Key':self.store.key if key else ''}))
        try:
            for route in ['/api/export-zip','/api/delete-invoices','/api/restore-invoices']:
                with self.assertRaises(urllib.error.HTTPError) as error:post(route,key=False)
                self.assertEqual(error.exception.code,403)
            with post('/api/export-zip') as response:
                self.assertEqual(response.headers['Content-Type'],'application/zip')
                with ZipFile(io.BytesIO(response.read())) as archive:self.assertEqual(len(archive.namelist()),2)
            with post('/api/delete-invoices') as response:self.assertEqual(json.load(response)['deleted'],2)
            with post('/api/restore-invoices') as response:self.assertEqual(json.load(response)['restored'],2)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':
    unittest.main(verbosity=2)
