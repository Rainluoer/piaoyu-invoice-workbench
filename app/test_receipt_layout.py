"""只删除指定占位文字和尾部空段落，保证金额和其他文档部件不受影响。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from docx import Document
from receipt_layout import clean_receipt


class ReceiptLayoutTests(unittest.TestCase):
    def test_cleaning_is_narrow_preserves_other_parts_and_is_idempotent(self):
        with TemporaryDirectory() as folder:
            source, output = Path(folder) / 'source.docx', Path(folder) / 'clean.docx'
            doc = Document()
            doc.add_paragraph('浙江大学科研材料入库单')
            header = doc.add_table(rows=4, cols=2)
            header.cell(0, 0).text = '项目（课题）名称及代码：'
            header.cell(0, 1).text = '2026 年 10 月 17 日'
            header.cell(1, 0).text = 'XXIJ49项目研发'
            header.cell(2, 0).text = 'PRJ-VSM23YS6'
            header.cell(3, 0).text = '实际填写的其他项目'
            table = doc.add_table(rows=1, cols=2)
            table.cell(0, 0).text = '金额合计'
            table.cell(0, 1).text = '334.39'
            doc.add_paragraph('注：采购与验收不能为同一人')
            doc.add_paragraph('')
            doc.add_paragraph('').add_run().add_break()
            doc.save(source)
            original = source.read_bytes()
            clean_receipt(source, output)
            self.assertEqual(source.read_bytes(), original)
            with ZipFile(source) as old, ZipFile(output) as new:
                self.assertEqual(old.namelist(), new.namelist())
                for part in old.namelist():
                    if part != 'word/document.xml':
                        self.assertEqual(old.read(part), new.read(part), part)
                xml = new.read('word/document.xml').decode()
                self.assertNotIn('XXIJ49', xml)
                self.assertNotIn('PRJ-VSM23YS6', xml)
                self.assertIn('334.39', xml)
            changed = Document(output)
            self.assertEqual(len(changed.tables[0].rows), 4)
            self.assertEqual(changed.tables[0].cell(1, 0).text, '')
            self.assertEqual(changed.tables[0].cell(2, 0).text, '')
            self.assertEqual(changed.paragraphs[-1].text, '注：采购与验收不能为同一人')
            before = output.read_bytes()
            self.assertFalse(clean_receipt(output))
            self.assertEqual(before, output.read_bytes())

    def test_workbench_template_has_no_presets_or_trailing_empty_paragraphs(self):
        path = Path(__file__).parent / 'assets' / '入库单模板.docx'
        doc = Document(path)
        self.assertEqual(len(doc.tables[0].rows), 3)
        self.assertEqual(doc.tables[0].cell(1, 0).text, '')
        self.assertEqual(doc.tables[0].cell(2, 0).text, '')
        self.assertIn('项目（课题）名称及代码', doc.tables[0].cell(0, 0).text)
        self.assertTrue(doc.paragraphs[-1].text.strip())
        self.assertEqual(len(doc.tables[1].rows), 10)

    def test_old_layout_restores_two_blank_project_rows(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'old.docx'
            doc = Document()
            header = doc.add_table(rows=1, cols=2)
            header.cell(0, 0).text = '项目（课题）名称及代码：'
            doc.add_paragraph('末尾注释')
            doc.save(path)
            clean_receipt(path, blank_rows_from=Path(__file__).parent / 'assets' / '入库单模板.docx')
            updated = Document(path)
            self.assertEqual(len(updated.tables[0].rows), 3)
            self.assertEqual(updated.tables[0].cell(1, 0).text, '')
            self.assertEqual(updated.tables[0].cell(2, 0).text, '')
            self.assertEqual(updated.paragraphs[-1].text, '末尾注释')


if __name__ == '__main__':
    unittest.main(verbosity=2)
