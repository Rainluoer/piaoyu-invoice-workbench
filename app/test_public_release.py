"""发布包的独立运行、可移植路径、来源与模板隐私边界。"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import server


class PublicReleaseTests(unittest.TestCase):
    def test_receipts_do_not_require_a_codex_home_installation(self):
        with TemporaryDirectory() as directory, patch('receipt_jobs.Path.home', side_effect=AssertionError('must not access home')):
            root = Path(directory)
            store = server.Store(root, root / 'app/data')
            try:
                self.assertEqual(store.receipts.skill_dir, server.BASE / 'vendor/ruku')
                self.assertTrue(store.receipts.availability()['available'])
            finally:
                store.db.close()

    def test_vendored_scripts_match_the_recorded_upstream_hashes(self):
        vendor = server.BASE / 'vendor/ruku'
        record = json.loads((vendor / 'UPSTREAM.json').read_text(encoding='utf-8'))
        for name, expected in record['scripts_sha256'].items():
            self.assertEqual(hashlib.sha256((vendor / 'scripts' / name).read_bytes()).hexdigest(), expected)
        self.assertIn('2026 3450530868', (vendor / 'LICENSE').read_text(encoding='utf-8'))

    def test_public_template_has_no_personal_metadata_or_external_relationships(self):
        for template in ('入库单模板.docx', '统计表格模板.docx'):
            with self.subTest(template=template), ZipFile(server.BASE / 'assets' / template) as package:
                for name in ('docProps/core.xml', 'docProps/custom.xml'):
                    self.assertEqual(len(ET.fromstring(package.read(name))), 0)
                self.assertFalse(any(part.startswith(('word/comments', 'word/people', 'word/embeddings', 'docProps/thumbnail')) for part in package.namelist()))
                for name in package.namelist():
                    if name.endswith('.rels'):
                        self.assertFalse(any(node.attrib.get('TargetMode') == 'External' for node in ET.fromstring(package.read(name))))

    def test_startup_uses_project_invoice_directory_and_explicit_ignore_paths(self):
        with TemporaryDirectory() as directory, \
             patch('sys.argv', ['server.py', '--no-browser', '--ignore-dir', directory]), \
             patch('server.IGNORED_DIRECTORIES', ()), \
             patch('server.LocalServer') as http, patch('server.Store') as store:
            http.return_value.serve_forever.side_effect = KeyboardInterrupt
            server.main()
            store.assert_called_once_with(server.BASE.parent / 'invoices', server.BASE / 'data')
            self.assertEqual(server.IGNORED_DIRECTORIES, (Path(directory).resolve(),))
            http.return_value.server_close.assert_called_once()
            store.return_value.db.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
