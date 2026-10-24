"""合成分层文字、相连数值、旋转水印及数值冲突，不保存真实发票。"""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pdfplumber
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from invoice_company import company_from_pdf, company_needs_repair
from invoice_extract import check_invoice, extract_layout_invoice, repair_extraction, ExtractionRepairError
from pdf_layout import layout_text
from server import Store
from statistics_table import rows_from_invoices, validate_rows


def layered_invoice(path, conflict=False, discount=False, invoice_no='12345678901234567890'):
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    p = canvas.Canvas(str(path), pagesize=(600, 400))
    p.setFont('STSong-Light', 8)
    # 先画模板，再画数值；源文字流中标签和值分离。
    for x, y, value in ((350,370,'发票号码：'),(350,350,'开票日期：'),
                        (20,320,'购'),(300,320,'销'),(20,300,'名称：'),(300,300,'名称：'),
                        (20,285,'买'),(300,285,'售'),(20,270,'方'),(300,270,'方'),
                        (20,240,'项目名称'),(150,240,'规格型号'),(230,240,'单位'),
                        (280,240,'数量'),(310,240,'单价'),(410,240,'金额'),(480,240,'税率'),(530,240,'税额'),
                        (20,170,'合计'),(20,150,'价税合计（大写）'),(410,150,'（小写）')):
        p.drawString(x,y,value)
    for x,y,value in ((400,370,invoice_no),(400,350,'2026年10月19日'),
                      (60,300,'合成大学'),(340,300,'合成材料有限公司'),
                      (20,220,'*材料*测试打印'),(20,205,'耗材PLA白'),(230,220,'件'),
                      (280,220,'3'),(280+pdfmetrics.stringWidth('3','STSong-Light',8)+2.5,220,'155.3398058252427'),
                      (410,220,'466.02'),(480,220,'3%'),(530,220,'13.98'),
                      (410,170,'466.00' if discount else '466.02'),(530,170,'13.98'),(450,150,'¥481.00' if conflict else '¥479.98' if discount else '¥480.00')):
        p.drawString(x,y,value)
    if discount:
        for x,y,value in ((20,190,'*材料*测试打印'),(20,180,'耗材PLA白'),(410,190,'-0.02'),(480,190,'3%'),(530,190,'0.00')):
            p.drawString(x,y,value)
    p.saveState();p.translate(590,350);p.rotate(-90);p.drawString(0,0,'下载次数：1');p.restoreState()
    p.save()


class LayoutExtractionTests(unittest.TestCase):
    def test_explicit_discount_is_kept_and_checked_against_same_product(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'001.pdf';layered_invoice(path,discount=True)
            result=extract_layout_invoice(path)
            self.assertEqual(result['total'],479.98)
            self.assertEqual(result['discounts'][0]['amount'],-.02)
            self.assertEqual(result['items'][0]['quantity'],3)
            bad=copy.deepcopy(result);bad['discounts'][0]['product']='另一个商品'
            with self.assertRaisesRegex(ValueError,'归属'):check_invoice(bad)
            bad=copy.deepcopy(result);bad['discounts'][0]['amount']=-1
            with self.assertRaisesRegex(ValueError,'价税合计'):check_invoice(bad)

    def test_batch_failure_keeps_later_repaired_invoice_and_manual_draft(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'工具';folder.mkdir()
            first=folder/'a-conflict.pdf';later=folder/'b-layout.pdf'
            layered_invoice(first,conflict=True);layered_invoice(later,invoice_no='12345678901234567891')
            store=Store(root,root/'app/data')
            try:
                store.scan();rows=sorted(store.state()['invoices'],key=lambda r:r['filename'])
                job=store.receipts.create([r['id'] for r in rows],background=False)
                self.assertEqual(job['status'],'failed')
                data=json.loads(Path(job['data_path']).read_text(encoding='utf-8'))
                self.assertIn('_extraction_error',data[0])
                self.assertEqual(data[1]['items'][0]['product'],'测试打印耗材PLA白')
                self.assertIn('002.pdf',job['layout_repaired_files'])
                manual=store.receipts.manual_data(job['id'])['invoices']
                self.assertFalse(manual[0]['extracted_complete'])
                self.assertTrue(manual[1]['extracted_complete'])
                # 模拟旧失败任务只有原脚本结果；GET 仍能补读后续特殊发票。
                Path(job['data_path']).write_text(json.dumps([dict(original_filename='001.pdf'),dict(original_filename='002.pdf')]),encoding='utf-8')
                old=store.receipts.manual_data(job['id'])['invoices']
                self.assertEqual(old[1]['items'][0]['quantity'],'3')
                old[1]['items'][0]['product']='用户修改的品名'
                store.receipts.save_manual(job['id'],old)
                self.assertEqual(store.receipts.manual_data(job['id'])['invoices'][1]['items'][0]['product'],'用户修改的品名')
            finally:store.db.close()

    def test_layers_rotated_watermark_long_price_and_product_continuation(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'001.pdf';layered_invoice(path)
            with pdfplumber.open(path) as pdf:
                self.assertNotIn('下载次数',layout_text(pdf.pages[0]))
            result=extract_layout_invoice(path)
            self.assertEqual(result['total'],480)
            item=result['items'][0]
            self.assertEqual((item['product'],item['quantity'],item['unit'],item['spec']),('测试打印耗材PLA白',3,'件',''))
            self.assertEqual(item['unit_price'],155.3398058252427)
            self.assertEqual(company_from_pdf(path),'合成材料有限公司')
            repaired,files=repair_extraction([{'original_filename':'001.pdf'}],path.parent,['001.pdf'])
            self.assertEqual(files,['001.pdf']);self.assertEqual(repaired[0]['items'],result['items'])

    def test_conflicts_do_not_pass_as_complete_extraction(self):
        good=dict(invoice_no='12345678901234567890',total=480,items=[dict(product='材料',quantity=3,unit_price=155.3398058252427,amount=466.02,tax=13.98)])
        check_invoice(good)
        for field,value in (('quantity',3155),('unit_price','nan'),('tax',0),('amount',460)):
            bad=copy.deepcopy(good);bad['items'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):check_invoice(bad)
        with TemporaryDirectory() as directory:
            path=Path(directory)/'001.pdf';layered_invoice(path,conflict=True)
            with self.assertRaisesRegex(ValueError,'未通过校验'):
                repair_extraction([],path.parent,['001.pdf'])

    def test_poisoned_company_is_repaired_and_manual_company_is_preserved(self):
        expected=rows_from_invoices([dict(total=480,items=[dict(product='材料')])],{'001.pdf':'合成材料有限公司'})
        submitted=copy.deepcopy(expected);submitted[0]['company']='载次数合成材料有限公司'
        self.assertTrue(company_needs_repair(submitted[0]['company']))
        self.assertEqual(validate_rows(submitted,expected)[0]['company'],'合成材料有限公司')
        expected[0]['company']=''
        with self.assertRaisesRegex(ValueError,'水印'):validate_rows(submitted,expected)
        submitted[0]['company']='用户确认公司'
        self.assertEqual(validate_rows(submitted,expected)[0]['company'],'用户确认公司')

    def test_real_three_script_pipeline_and_migration_preserve_original(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'工具';folder.mkdir();path=folder/'分层票.pdf';layered_invoice(path)
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            store=Store(root,root/'app/data')
            try:
                store.scan();row=store.state()['invoices'][0]
                self.assertEqual((row['amount_cents'],row['seller']),(48000,'合成材料有限公司'))
                store.db.execute("UPDATE invoices SET title='原品名',seller='载次数合成材料有限公司' WHERE id=?",(row['id'],));store.db.commit()
                store.company_parser_changed=True
                with patch('server.parse_text',side_effect=AssertionError('不应重读其他字段')):store.scan()
                saved=store.state()['invoices'][0];self.assertEqual(saved['title'],'原品名');self.assertEqual(saved['seller'],'合成材料有限公司')
                job=store.receipts.create([row['id']],background=False)
                self.assertEqual(job['status'],'done',job.get('error'))
                self.assertFalse(job.get('statistics_error'))
                self.assertEqual(job.get('layout_repaired_files'),['001.pdf'])
                self.assertEqual(job['statistics_rows'][0]['company'],'合成材料有限公司')
                self.assertEqual(job['statistics_rows'][0]['name'],'测试打印耗材PLA白')
                self.assertEqual(job['statistics_rows'][0]['price'],'480.00')
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
            finally:store.db.close()

    def test_ambiguous_pdf_enters_manual_without_generating_document(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'工具';folder.mkdir();path=folder/'冲突.pdf';layered_invoice(path,conflict=True)
            store=Store(root,root/'app/data')
            try:
                store.scan();row=store.state()['invoices'][0]
                job=store.receipts.create([row['id']],background=False)
                self.assertEqual(job['status'],'failed');self.assertTrue(job['manual_available'])
                self.assertFalse(Path(job['output']).exists())
            finally:store.db.close()


if __name__=='__main__':unittest.main(verbosity=2)
