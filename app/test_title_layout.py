"""完整票号/日期/总额不能证明商品品名已经正确分列。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pdfplumber
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from server import Store, parse_text


def lodging_invoice(path):
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    page = canvas.Canvas(str(path), pagesize=(600, 400))
    page.setFont('STSong-Light', 8)
    fields = [
        (20, 380, '电子发票（普通发票）'),
        (20, 360, '发票号码：12345678901234567890'),
        (20, 340, '开票日期：2026年10月29日'),
        (20, 300, '项目名称'), (150, 300, '规格型号'),
        (230, 300, '单 位'), (280, 300, '数 量'),
        (330, 300, '单 价'), (410, 300, '金 额'),
        (480, 300, '税率/征收率'), (550, 300, '税 额'),
        (20, 275, '*生产生活服务*住宿费'), (230, 275, '天'),
        (280, 275, '1'),
        # 原始 extract_text 默认容差会把数量和单价拼成一个数字。
        (280 + pdfmetrics.stringWidth('1', 'STSong-Light', 8) + 2.5, 275, '264.1509433962264'),
        (410, 275, '264.15'), (480, 275, '6%'), (550, 275, '15.85'),
        (20, 220, '合 计'), (410, 220, '264.15'), (550, 220, '15.85'),
        (20, 200, '价税合计（大写）'), (410, 200, '（小写）¥280.00')
    ]
    for x, y, text in fields:
        page.drawString(x, y, text)
    page.save()


class TitleLayoutTests(unittest.TestCase):
    def test_complete_basic_fields_still_check_product_column(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'synthetic-lodging.pdf'
            lodging_invoice(path)
            with pdfplumber.open(path) as pdf:
                raw = parse_text(pdf.pages[0].extract_text(), path.name)
            self.assertEqual(raw['invoice_no'], '12345678901234567890')
            self.assertEqual(raw['date'], '2026-10-29')
            self.assertEqual(raw['amount_cents'], 28000)
            self.assertEqual(raw['title'], '生产生活服务 · 住宿费 天')
            store = Store(root, root / 'app/data')
            try:
                self.assertEqual(store.scan()['added'], 1)
                row = store.state()['invoices'][0]
                self.assertEqual(row['title'], '生产生活服务 · 住宿费')
                self.assertEqual(row['invoice_no'], raw['invoice_no'])
                self.assertEqual(row['date'], raw['date'])
                self.assertEqual(row['amount_cents'], raw['amount_cents'])
                self.assertEqual(row['warning'], '')
            finally:
                store.db.close()

    def test_scan_and_restart_preserve_manual_title_and_other_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'synthetic-lodging.pdf'
            lodging_invoice(path)
            store = Store(root, root / 'app/data')
            try:
                store.scan()
                row = store.state()['invoices'][0]
                store.update([row['id']], {'title': '自定义住宿标题', 'category': '住宿费',
                                          'notes': '保留备注', 'status': '已报销', 'project': '测试项目'})
                store.scan()
                store.db.close()
                store = Store(root, root / 'app/data')
                store.scan()
                saved = store.state()['invoices'][0]
                for key in ('title', 'category', 'notes', 'status', 'project'):
                    self.assertEqual(saved[key], {'title': '自定义住宿标题', 'category': '住宿费',
                                                 'notes': '保留备注', 'status': '已报销', 'project': '测试项目'}[key])
            finally:
                store.db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
