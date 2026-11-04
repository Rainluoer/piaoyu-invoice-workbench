"""批量打印：真实合成 PDF/图片渲染、完整页序和原件保护。"""
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from server import Handler, LocalServer, Store


class InvoicePrintTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.pdf = self.root / '两页发票.pdf'
        canvas = Canvas(str(self.pdf), pagesize=(144, 72))
        for color in ((1, 0, 0), (0, 0, 1)):
            canvas.setFillColorRGB(*color)
            canvas.rect(0, 0, 144, 72, stroke=0, fill=1)
            canvas.showPage()
        canvas.save()
        self.picture = self.root / '图片.png'
        Image.new('RGB', (24, 12), 'green').save(self.picture)
        self.jpeg = self.root / '旋转.jpg'
        exif = Image.Exif(); exif[274] = 6
        Image.new('RGB', (24, 12), 'yellow').save(self.jpeg, exif=exif)
        self.store = Store(self.root, self.root / 'app/data')
        self.store.scan()
        self.rows = {Path(r['path']): r for r in self.store.state()['invoices']}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_manifest_order_all_pages_ids_and_originals_unchanged(self):
        before = [tuple(r) for r in self.store.db.execute('SELECT * FROM invoices ORDER BY id')]
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.rows}
        ids = [self.rows[p]['id'] for p in (self.picture, self.pdf, self.jpeg, self.pdf)]
        manifest = self.store.print_manifest(ids)
        self.assertEqual(manifest['invoice_count'], 3)
        self.assertEqual([(r['filename'], r['page']) for r in manifest['pages']],
                         [('图片.png', 1), ('两页发票.pdf', 1), ('两页发票.pdf', 2), ('旋转.jpg', 1)])
        for p, old in hashes.items():
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), old)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM invoices ORDER BY id')], before)

    def test_print_renders_complete_pdf_pages_and_exif_image_orientation(self):
        row = self.rows[self.pdf]
        for page, color in ((1, (255, 0, 0)), (2, (0, 0, 255))):
            with Image.open(io.BytesIO(self.store.print_page(row['id'], row['sha256'], page))) as image:
                self.assertEqual(image.size, (400, 200))  # 200 dpi，保持原页比例。
                self.assertEqual(image.convert('RGB').getpixel((200, 100)), color)
        row = self.rows[self.jpeg]
        with Image.open(io.BytesIO(self.store.print_page(row['id'], row['sha256'], 1))) as image:
            self.assertEqual(image.size, (12, 24))
        row = self.rows[self.picture]
        with Image.open(io.BytesIO(self.store.print_page(row['id'], row['sha256'], 1))) as image:
            self.assertEqual(image.size, (24, 12))

    def test_changed_missing_deleted_unknown_and_invalid_pages_fail(self):
        row = self.rows[self.pdf]
        for page in (0, 3):
            with self.assertRaisesRegex(ValueError, '页码'):
                self.store.print_page(row['id'], row['sha256'], page)
        with self.assertRaisesRegex(ValueError, '记录已变化'):
            self.store.print_page(row['id'], 'stale', 1)
        with self.assertRaises(ValueError):
            self.store.print_manifest([row['id'], 'unknown'])
        row = self.rows[self.picture]
        self.picture.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '文件已变化'):
            self.store.print_manifest([row['id']])
        self.picture.unlink()
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.store.print_manifest([row['id']])
        row = self.rows[self.jpeg]
        self.store.delete_invoices([row['id']])
        with self.assertRaises(ValueError):
            self.store.print_manifest([row['id']])

    def test_corrupt_and_oversized_print_is_rejected(self):
        bad = self.root / '损坏.pdf'
        bad.write_bytes(b'not a pdf')
        self.store.scan()
        row = next(r for r in self.store.state()['invoices'] if r['path'] == str(bad))
        with self.assertRaisesRegex(ValueError, '损坏.pdf.*无法准备打印'):
            self.store.print_manifest([row['id']])
        with patch('server.MAX_ZIP', 1), self.assertRaisesRegex(ValueError, '200 MB'):
            self.store.print_manifest([self.rows[self.pdf]['id']])

    def test_http_print_manifest_pages_and_assets(self):
        server = LocalServer(('127.0.0.1', 0), Handler)
        server.store = self.store
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f'http://127.0.0.1:{server.server_port}'
        row = self.rows[self.pdf]
        def post(key):
            return opener.open(urllib.request.Request(base + '/api/print-invoices',
                data=json.dumps({'ids': [row['id']]}).encode(), headers={'X-Workbench-Key': key}))
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                post('')
            self.assertEqual(error.exception.code, 403)
            with post(self.store.key) as response:
                self.assertEqual(len(json.load(response)['pages']), 2)
            with opener.open(f'{base}/api/print-page/{row["id"]}?page=2&sha={row["sha256"]}') as response:
                self.assertEqual(response.headers['Content-Type'], 'image/png')
                self.assertTrue(response.read().startswith(b'\x89PNG'))
            for asset in ('print.html', 'print.js', 'print.css'):
                with opener.open(base + '/' + asset) as response:
                    self.assertTrue(response.read())
                    self.assertIn('blob:', response.headers['Content-Security-Policy'])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
