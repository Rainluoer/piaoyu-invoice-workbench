"""内容分类覆盖单文件、混合 ZIP、目录冲突及人工分类保留。"""
import base64
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from invoice_category import classify_invoice
from server import CATEGORIES, Store


def invoice_pdf(number, item, heading='电子发票（普通发票）', extra=''):
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    output = io.BytesIO()
    page = canvas.Canvas(output, pagesize=(600, 400))
    page.setFont('STSong-Light', 10)
    for index, line in enumerate((heading, '发票号码：' + number, '开票日期：2026年10月30日',
                                  item, '（小写）¥10.00', extra)):
        page.drawString(20, 380 - index * 25, line)
    page.save()
    return output.getvalue()


class InitialCategoryRulesTests(unittest.TestCase):
    def test_explicit_railway_heading_wins_over_source_folder(self):
        for heading in ('电子发票（铁路电子客票）', '电子发票(铁路电子客票)',
                        '电子发票（ 铁 路 电 子 客 票 ）', '铁路电子客票'):
            with self.subTest(heading=heading):
                self.assertEqual(classify_invoice('工具/普通.zip', heading, filename='001.pdf'), '铁路客票')

    def test_product_examples_use_only_existing_categories(self):
        cases = [
            ('*生产生活服务*住宿费 天 1264.1509433962264 264.15 6% 15.85', '住宿费'),
            ('*住宿服务*标准间客房费', '住宿费'),
            ('*交通运输服务*客运服务费', '出行交通'),
            ('*运输服务*旅客运输服务', '出行交通'),
            ('*生活服务*停车费', '出行交通'),
            ('*化学试剂*乙醇', '科研材料'),
            ('*生物试剂*缓冲液', '科研材料'),
            ('*计算机配套产品*USB网卡', '科研材料'),
            ('*计算机配件*USB转接头', '科研材料'),
            ('*金属制品*内六角螺丝', '科研材料'),
            ('*电线电缆*HDMI连接线', '科研材料'),
            ('*仪器仪表*电子测量仪', '科研材料'),
            ('*光学仪器*USB摄像头', '科研材料'),
            ('*塑料制品*PLA白色耗材', '科研材料'),
            ('*塑料制品*pla耗材1.75mm', '科研材料'),
            ('*塑料制品*Pla+', '科研材料'),
            ('*塑料制品*eSUN PLA+', '科研材料'),
            ('*服务*Display维护服务', '其他票据'),
            ('*专业技术服务*技术服务', '其他票据'),
            ('*餐饮服务*工作餐', '其他票据')
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                actual = classify_invoice(text='电子发票（普通发票）\n' + item)
                self.assertIn(actual, CATEGORIES)
                self.assertEqual(actual, expected)

    def test_sellers_buyers_and_remarks_are_not_product_evidence(self):
        for extra in ('销售方：合成酒店管理有限公司', '购买方：铁路研究所',
                      '备注：出差高铁住宿费', '备注：采购试剂和电线电缆'):
            with self.subTest(extra=extra):
                text = '电子发票（普通发票）\n*餐饮服务*餐费\n' + extra
                self.assertEqual(classify_invoice('工具/餐费.pdf', text, filename='酒店住宿费.pdf'), '其他票据')
        text = '电子发票（普通发票）\n*专业技术服务*技术服务\n备注：铁路电子客票'
        self.assertEqual(classify_invoice(text=text, title='铁路电子客票'), '其他票据')

    def test_content_beats_filename_and_conflicting_items_stay_other(self):
        self.assertEqual(classify_invoice('高铁票/错误名称.pdf', '*生产生活服务*住宿费', filename='高铁票.pdf'), '住宿费')
        self.assertEqual(classify_invoice(text='*生产生活服务*住宿费\n*餐饮服务*餐费'), '其他票据')
        self.assertEqual(classify_invoice(text='*交通运输服务*客运服务费\n*生产生活服务*住宿费'), '其他票据')

    def test_name_directory_and_layout_title_fallbacks(self):
        for name, expected in [('铁路电子客票.png', '铁路客票'), ('高铁票.pdf', '铁路客票'),
                               ('住宿费.jpg', '住宿费'), ('网约车发票.pdf', '出行交通'),
                               ('酒店管理有限公司.pdf', '其他票据'),
                               ('摄像头.pdf', '科研材料'), ('eSUN PLA白色.pdf', '科研材料')]:
            with self.subTest(name=name):
                self.assertEqual(classify_invoice(filename=name), expected)
        self.assertEqual(classify_invoice(source='已报销/工具/材料.png'), '科研材料')
        self.assertEqual(classify_invoice(source='高铁票/001.png'), '铁路客票')
        self.assertEqual(classify_invoice(source='车票/001.png'), '出行交通')
        self.assertEqual(classify_invoice(title='计算机配套产品 · USB网卡'), '科研材料')


class InitialCategoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root, self.root / 'app/data')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_mixed_zip_is_classified_per_invoice_and_archive_is_unchanged(self):
        fixtures = [
            ('rail.pdf', '12345678901234567890', '', '电子发票（铁路电子客票）', '铁路客票'),
            ('stay.pdf', '12345678901234567891', '*生产生活服务*住宿费', '电子发票（普通发票）', '住宿费'),
            ('ride.pdf', '12345678901234567892', '*交通运输服务*客运服务费', '电子发票（普通发票）', '出行交通'),
            ('lab.pdf', '12345678901234567893', '*电线电缆*HDMI连接线', '电子发票（普通发票）', '科研材料'),
            ('camera.pdf', '12345678901234567897', '*光学仪器*摄像头', '电子发票（普通发票）', '科研材料'),
            ('pla.pdf', '12345678901234567898', '*塑料制品*pla白色', '电子发票（普通发票）', '科研材料'),
            ('meal.pdf', '12345678901234567894', '*餐饮服务*餐费', '电子发票（普通发票）', '其他票据')
        ]
        archive = self.root / '普通混合票据.zip'
        with ZipFile(archive, 'w') as bundle:
            for name, number, item, heading, _ in fixtures:
                bundle.writestr(name, invoice_pdf(number, item, heading))
        original = archive.read_bytes()
        self.assertEqual(self.store.scan()['added'], len(fixtures))
        rows = {row['invoice_no']: row for row in self.store.state()['invoices']}
        for _, number, _, _, expected in fixtures:
            self.assertEqual(rows[number]['category'], expected)
            self.assertEqual(rows[number]['amount_cents'], 1000)
            self.assertEqual(rows[number]['status'], '待报销')
        self.assertEqual(archive.read_bytes(), original)

    def test_direct_upload_and_manual_type_survive_rescan_restart(self):
        content = invoice_pdf('12345678901234567895', '', '电子发票（铁路电子客票）')
        self.assertEqual(self.store.upload('001.pdf', base64.b64encode(content).decode())['added'], 1)
        row = self.store.state()['invoices'][0]
        self.assertEqual(row['category'], '铁路客票')
        self.assertEqual(self.store.category_history(row['id']), [])
        self.store.update([row['id']], {'category': '其他票据', 'notes': '人工核对', 'status': '已报销'})
        # 即使内容重新解析，也不能重新自动分类已有台账。
        path = Path(row['path'])
        self.store.parser_changed = True
        self.store.ingest(path, row['source'], {})
        self.store.db.commit()
        self.store.scan()
        self.store.db.close()
        self.store = Store(self.root, self.root / 'app/data')
        self.store.scan()
        saved = self.store.state()['invoices'][0]
        self.assertEqual(saved['category'], '其他票据')
        self.assertEqual(saved['notes'], '人工核对')
        self.assertEqual(saved['status'], '已报销')
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(len(self.store.category_history(row['id'])), 1)

    def test_existing_automatic_categories_are_not_migrated(self):
        path = self.root / '旧票.pdf'
        path.write_bytes(invoice_pdf('12345678901234567896', '*生产生活服务*住宿费'))
        self.store.scan()
        row = self.store.state()['invoices'][0]
        self.store.db.execute("UPDATE invoices SET category='其他票据',manual=0 WHERE id=?", (row['id'],))
        self.store.db.commit()
        self.store.parser_changed = True
        self.store.ingest(path, '旧票.pdf', {})
        self.store.db.commit()
        self.assertEqual(self.store.state()['invoices'][0]['category'], '其他票据')


if __name__ == '__main__':
    unittest.main(verbosity=2)
