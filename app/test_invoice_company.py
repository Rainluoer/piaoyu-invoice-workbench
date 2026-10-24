"""销售方识别不混入购买方，统计表空公司自动补全，手填公司保留。"""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from invoice_company import seller_from_text, company_from_pdf
from server import Store
from statistics_table import rows_from_invoices, validate_rows
import test_receipt_jobs as fixtures


def overlay_invoice(path):
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    pdf = canvas.Canvas(str(path), pagesize=(600, 400))
    pdf.setFont('STSong-Light', 10)
    for x, words in ((20, '购买方信息'), (300, '销售方信息')):
        for index, char in enumerate(words):
            pdf.drawString(x, 300 - index * 12, char)
    pdf.drawString(40, 300, '名称：')
    pdf.drawString(320, 300, '名称：')
    pdf.drawString(40, 260, '统一社会信用代码：')
    pdf.drawString(320, 260, '统一社会信用代码：')
    pdf.drawString(80, 300, '合成测试大学')
    pdf.drawString(360, 300, '合成测试材料有限公司')
    pdf.save()


class InvoiceCompanyTests(unittest.TestCase):
    def test_direct_parallel_and_vertical_labels_select_seller_not_buyer(self):
        cases = [
            '购买方名称：合成测试大学\n销售方名称：合成测试材料有限公司',
            '销售方：合成测试材料有限公司 统一社会信用代码：TEST123456789012',
            '购 名称：合成测试大学 销 名称：合成测试材料有限公司\n买 售\n方 方',
            '购 销\n买 名 称 合成测试大学 售 名 称 合成测试材料有限公司\n: :\n方 方',
            '销售方名称：\n合成测试材料有限公司',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(seller_from_text(text), '合成测试材料有限公司')
        for text in ('购买方名称：合成测试大学', '购 名称：合成测试大学 销 名称：\n买 售\n方 方',
                     '项目名称：合成测试有限公司产品\n开票人：某公司',
                     '销售方：\n统一社会信用代码：TEST123456789012'):
            self.assertEqual(seller_from_text(text), '')

    def test_pdf_overlay_values_are_read_using_name_coordinates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'overlay.pdf'
            overlay_invoice(path)
            self.assertEqual(company_from_pdf(path), '合成测试材料有限公司')
            path.write_bytes(b'invalid PDF')
            self.assertEqual(company_from_pdf(path), '')

    def test_reparse_fills_missing_company_but_preserves_manual_invoice_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '工具').mkdir()
            path = root / '工具' / 'overlay.pdf'
            overlay_invoice(path)
            store = Store(root, root / 'app/data')
            try:
                store.scan()
                row = store.state()['invoices'][0]
                store.update([row['id']], {'title':'手工品名', 'amount':'12.34', 'status':'已报销', 'project':'保留项目', 'notes':'保留备注'})
                store.db.execute("UPDATE invoices SET seller='' WHERE id=?", (row['id'],))
                store.db.commit()
                store.parser_changed = True
                store.scan()
                saved = store.state()['invoices'][0]
                self.assertEqual(saved['seller'], '合成测试材料有限公司')
                self.assertEqual([saved[k] for k in ('title','amount_cents','status','project','notes')], ['手工品名',1234,'已报销','保留项目','保留备注'])
                store.update([row['id']], {'seller':'用户确认的供应商'})
                store.parser_changed = True
                store.scan()
                self.assertEqual(store.state()['invoices'][0]['seller'], '用户确认的供应商')
            finally:
                store.db.close()

    def test_company_migration_does_not_reparse_other_automatic_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '工具').mkdir()
            path = root / '工具' / 'overlay.pdf'
            overlay_invoice(path)
            store = Store(root, root / 'app/data')
            try:
                store.scan()
                row = store.state()['invoices'][0]
                store.db.execute("UPDATE invoices SET title='保留原有自动品名',amount_cents=9876,seller='' WHERE id=?", (row['id'],))
                store.db.execute("DELETE FROM meta WHERE key='company_parser_version'")
                store.db.commit()
                store.company_parser_changed = True
                with patch('server.parse_text', side_effect=AssertionError('must not reparse fields')):
                    store.scan()
                saved = store.state()['invoices'][0]
                self.assertEqual((saved['title'], saved['amount_cents'], saved['manual']), ('保留原有自动品名',9876,0))
                self.assertEqual(saved['seller'], '合成测试材料有限公司')
            finally:
                store.db.close()

    def test_company_autofills_empty_saved_rows_but_preserves_explicit_override(self):
        expected = rows_from_invoices([dict(total=12.34, items=[dict(product='测试材料')])], {'001.pdf':'识别的销售方'})
        submitted = copy.deepcopy(expected)
        submitted[0]['company'] = ''
        self.assertEqual(validate_rows(submitted, expected)[0]['company'], '识别的销售方')
        submitted[0]['company'] = '用户手动指定公司'
        self.assertEqual(validate_rows(submitted, expected)[0]['company'], '用户手动指定公司')


class StatisticsCompanyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReceiptJobTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store

    def test_old_task_uses_cached_invoice_text_and_preserves_saved_company(self):
        with patch('receipt_jobs.subprocess.run', side_effect=self.fixture.fake_script):
            job = self.store.receipts.create([self.fixture.row['id']], background=False)
        self.store.db.execute("UPDATE invoices SET text=? WHERE id=?", ('购 名称：合成测试大学 销 名称：合成测试材料有限公司', self.fixture.row['id']))
        self.store.db.commit()
        rows = self.store.receipts.statistics_data(job['id'])['rows']
        self.assertEqual(rows[0]['company'], '合成测试材料有限公司')
        rows[0]['company'] = '用户指定公司'
        self.store.receipts.save_statistics(job['id'], rows)
        self.assertEqual(self.store.receipts.statistics_data(job['id'])['rows'][0]['company'], '用户指定公司')

    def test_missing_original_uses_hash_matched_renamed_job_copy(self):
        with patch('receipt_jobs.subprocess.run', side_effect=self.fixture.fake_script):
            job = self.store.receipts.create([self.fixture.row['id']], background=False)
        expected_copy = Path(job['output']).parent / 'renamed.pdf'
        (Path(job['output']).parent / '001.pdf').rename(expected_copy)
        self.fixture.original.unlink()
        with patch('receipt_jobs.company_from_pdf', return_value='副本识别公司') as read:
            rows = self.store.receipts.statistics_data(job['id'])['rows']
        self.assertEqual(rows[0]['company'], '副本识别公司')
        read.assert_called_once_with(expected_copy)


if __name__ == '__main__':
    unittest.main(verbosity=2)
