"""行程单附件不进入发票列表、去重及导出，原件和历史记录保留。"""
import base64
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
import urllib.request
from zipfile import ZipFile

from server import Store, LocalServer, Handler, is_itinerary


class ItineraryVisibilityTests(unittest.TestCase):
    def test_existing_attachments_hide_before_dedup_and_survive_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                'a_行程单.png': b'same bytes', 'b_发票.png': b'same bytes',
                'c_renamed.png': b'renamed itinerary', 'd_普通票.png': b'invoice with note',
                'e_行程单发票.png': b'actual invoice',
            }
            for name, content in fixtures.items():
                (root / name).write_bytes(content)
            store = Store(root, root / 'app/data')
            try:
                store.scan()
                store.db.execute("UPDATE invoices SET text=? WHERE filename=?", ('打车——行程单\nAMAP ITINERARY\n申请时间：2026-10-29', 'c_renamed.png'))
                store.db.execute("UPDATE invoices SET text=? WHERE filename=?", ('普通资料\n备注：请附行程单', 'd_普通票.png'))
                store.db.execute("UPDATE invoices SET text=? WHERE filename=?", ('电子发票（普通发票）\n发票号码：12345678901234567890', 'e_行程单发票.png'))
                store.db.commit()
                store.scan()
                store.db.close()
                store = Store(root, root / 'app/data')
                rows = store.state()['invoices']
                self.assertEqual({r['filename'] for r in rows}, {'b_发票.png', 'd_普通票.png', 'e_行程单发票.png'})
                self.assertTrue(all(not r['duplicate_of'] for r in rows))
                self.assertEqual(store.db.execute('SELECT count(*) FROM invoices').fetchone()[0], 5)
                self.assertEqual(store.state()['trash'], [])
                self.assertTrue(all((root / name).read_bytes() == content for name, content in fixtures.items()))
                self.assertFalse(is_itinerary(dict(filename='机票行程单.pdf', text='电子发票（航空运输电子客票行程单）')))
            finally:
                store.db.close()

    def test_zip_repeat_scan_and_http_export_only_include_invoices(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root, root / 'app/data')
            archive = io.BytesIO()
            with ZipFile(archive, 'w') as package:
                package.writestr('订单_行程单.png', b'itinerary attachment')
                package.writestr('订单_发票.png', b'invoice original')
            encoded = base64.b64encode(archive.getvalue()).decode()
            http = None
            try:
                store.upload('合成票据.zip', encoded)
                store.scan()
                store.upload('合成票据.zip', encoded)
                visible = store.state()['invoices']
                self.assertEqual(len(visible), 1)
                self.assertIn('发票', visible[0]['filename'])
                hidden = dict(store.db.execute("SELECT * FROM invoices WHERE filename LIKE '%行程单%'").fetchone())
                self.assertTrue(Path(hidden['path']).is_file())
                with self.assertRaises(ValueError):
                    store.export_zip([hidden['id']])
                http = LocalServer(('127.0.0.1', 0), Handler)
                http.store = store
                worker = threading.Thread(target=http.serve_forever, daemon=True)
                worker.start()
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                base = f'http://127.0.0.1:{http.server_port}'
                with opener.open(base + '/api/state') as response:
                    self.assertEqual(len(json.load(response)['invoices']), 1)
                with opener.open(base + '/api/export') as response:
                    content = response.read().decode('utf-8-sig')
                    self.assertIn('发票.png', content)
                    self.assertNotIn('行程单', content)
            finally:
                if http:
                    http.shutdown(); http.server_close(); worker.join(timeout=3)
                store.db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
