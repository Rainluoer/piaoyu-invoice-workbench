import subprocess
import sys
import importlib

_REQUIRED_PACKAGES = {
    'docx': 'python-docx',
}


def _ensure_deps():
    for mod_name, pkg_name in _REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(mod_name)
        except ImportError:
            print(f'正在安装依赖 {pkg_name}...')
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg_name, '-q'])


_ensure_deps()

import json
import os
import shutil
import copy
import docx
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, Cm
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from datetime import datetime
import argparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_TEMPLATE = os.path.join(_SCRIPT_DIR, '..', 'template', '模板.docx')

_TOTAL_BASE = 8  # 模板中合计行的固定索引


def num_to_chinese_upper(num):
    digits = ['零', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖']
    units = ['', '拾', '佰', '仟']
    big_units = ['', '万', '亿']
    if num == 0:
        return '零元整'
    integer_part = int(num)
    decimal_part = round((num - integer_part) * 100)
    jiao = decimal_part // 10
    fen = decimal_part % 10
    result = ''
    num_str = str(integer_part)
    length = len(num_str)
    for i, digit in enumerate(num_str):
        d = int(digit)
        pos = length - i - 1
        if d != 0:
            result += digits[d] + units[pos % 4]
        elif result and not result.endswith('零'):
            result += '零'
        if pos > 0 and pos % 4 == 0 and result.rstrip('零'):
            result = result.rstrip('零') + big_units[pos // 4]
    result = result.rstrip('零')
    if jiao == 0 and fen == 0:
        result += '元整'
    else:
        result += '元'
        if jiao > 0:
            result += digits[jiao] + '角'
        if fen > 0:
            result += digits[fen] + '分'
    return result


def set_cell_text(cell, text, font_size=Pt(9), bold=False):
    """设置单元格文本、字号、居中，清除多余段落
    自动调整行高为 atLeast 防止文字遮挡
    """
    for extra_para in cell.paragraphs[1:]:
        extra_para._element.getparent().remove(extra_para._element)

    para = cell.paragraphs[0]
    para.alignment = docx.enum.text.WD_ALIGN_PARAGRAPH.CENTER

    for extra_run in para.runs[1:]:
        extra_run._element.getparent().remove(extra_run._element)

    if para.runs:
        run = para.runs[0]
    else:
        run = para.add_run('')

    run.text = text
    run.font.size = font_size
    run.font.name = '宋体'
    run._element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')
    run.font.bold = bold

    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

    # 确保所在行行高为 atLeast，允许文字换行
    row = cell._tc.getparent().getparent()
    trPr = row.find(qn('w:trPr'))
    if trPr is not None:
        trH = trPr.find(qn('w:trHeight'))
        if trH is not None and trH.get(qn('w:hRule')) != 'atLeast':
            trH.set(qn('w:hRule'), 'atLeast')


def _duplicate_data_row(table, total_physical_idx):
    """在合计行前插入新数据行，复制最后一行格式并清空内容，返回新行索引
    自动将行高规则改为 atLeast 防止文字遮挡
    """
    ref_idx = total_physical_idx - 1
    ref_tr = table.rows[ref_idx]._tr
    new_tr = copy.deepcopy(ref_tr)
    for tc in new_tr.findall(qn('w:tc')):
        for p in tc.findall(qn('w:p')):
            for r in p.findall(qn('w:r')):
                p.remove(r)
    # 改为 atLeast 行高（允许自动扩展）
    trPr = new_tr.find(qn('w:trPr'))
    if trPr is not None:
        trH = trPr.find(qn('w:trHeight'))
        if trH is not None:
            trH.set(qn('w:hRule'), 'atLeast')
    table.rows[total_physical_idx]._tr.addprevious(new_tr)
    return total_physical_idx


def _update_header_date(doc):
    """将入库单右上角日期改为制作入库单当天日期

    模板右上角日期是嵌套在项目信息表格(Table[0])内的 6 列小表格，
    每个字各占一格：'2026'/'年'/'6'/'月'/'8'/'日'
    """
    now = datetime.now()
    parts = [str(now.year), '年', str(now.month), '月', str(now.day), '日']

    # 在项目信息表格中定位嵌套的日期表格
    nested_tbl = None
    for row in doc.tables[0].rows:
        for cell in row.cells:
            tbl = cell._tc.find(qn('w:tbl'))
            if tbl is not None:
                nested_tbl = tbl
                break
        if nested_tbl is not None:
            break

    if nested_tbl is None:
        return

    tr = nested_tbl.find(qn('w:tr'))
    if tr is None:
        return

    tcs = tr.findall(qn('w:tc'))
    for tc, text in zip(tcs, parts):
        t = tc.find('.//' + qn('w:t'))
        if t is not None:
            t.text = text

    try:
        print(f'日期已更新为: {now.year}年{now.month}月{now.day}日')
    except UnicodeEncodeError:
        print(f'Date updated: {now.year}-{now.month}-{now.day}')


def main():
    parser = argparse.ArgumentParser(description='基于模板填写入库单')
    parser.add_argument('--template', default=_DEFAULT_TEMPLATE, help='模板docx文件路径（默认skill内template/模板.docx）')
    parser.add_argument('--data', required=True, help='发票数据JSON文件路径')
    parser.add_argument('--output', required=True, help='输出docx文件路径')
    args = parser.parse_args()

    with open(args.data, 'r', encoding='utf-8') as f:
        invoices = json.load(f)

    all_items = []
    for inv in invoices:
        items = inv.get('items', [])
        if not items:
            items = [{
                'category': inv.get('category', ''),
                'product': inv.get('product', ''),
                'spec': '',
                'unit': '个',
                'quantity': inv.get('quantity', 0),
                'unit_price': inv.get('unit_price', 0),
                'amount': inv.get('amount', 0)
            }]
        for item in items:
            item['invoice_no'] = inv.get('invoice_no', '')
            item['invoice_total'] = inv.get('total', 0)
            all_items.append(item)

    # 统计每张发票的商品数量（用于判断单/多商品发票）
    item_count_per_invoice = {}
    for item in all_items:
        inv_no = item.get('invoice_no', '')
        item_count_per_invoice[inv_no] = item_count_per_invoice.get(inv_no, 0) + 1

    invoice_totals = {}
    for item in all_items:
        invoice_no = item.get('invoice_no', '')
        if invoice_no not in invoice_totals:
            invoice_totals[invoice_no] = item.get('invoice_total', 0)
    grand_total = sum(invoice_totals.values())
    chinese = num_to_chinese_upper(grand_total)

    print(f'Grand total: {grand_total}')
    print(f'Chinese: {chinese}')
    print(f'Total items: {len(all_items)}')

    doc = docx.Document(args.template)

    # 更新右上角日期为制作入库单当天日期
    _update_header_date(doc)

    # 模板结构:
    #   Table[0]: 项目信息透明表格（自动保留，无需修改）
    #   Table[1]: 入库单主表格（10行×9列）
    #     0:编号左 1:编号右 2:品名 3:型号(规格) 4:单位
    #     5:入库数量 6:单价(元) 7:金额(元) 8:备注
    #     第8行：金额合计（大写）+ 小写金额
    #     第9行：签名栏
    table = doc.tables[1]

    total_idx = _TOTAL_BASE  # 合计行当前索引（插入新行后会后移）

    for i, item in enumerate(all_items):
        # 若超出当前数据行数，在合计行前插入新行
        if i + 1 >= total_idx:
            _duplicate_data_row(table, total_idx)
            total_idx += 1
        row = table.rows[i + 1]

        set_cell_text(row.cells[0], str(i + 1), Pt(9))
        set_cell_text(row.cells[1], str(i + 1), Pt(9))

        cat = item.get('category', '')
        prod = item.get('product', '')
        product = f'*{cat}*{prod}'
        prod_size = Pt(7) if len(product) > 25 else Pt(8) if len(product) > 15 else Pt(9)
        set_cell_text(row.cells[2], product, prod_size)

        spec = item.get('spec', '')
        spec_size = Pt(8) if len(spec) > 20 else Pt(9)
        set_cell_text(row.cells[3], spec, spec_size)

        unit = item.get('unit', '个')
        if not unit:
            unit = '个'
        set_cell_text(row.cells[4], unit, Pt(9))

        qty = item.get('quantity', 0)
        set_cell_text(row.cells[5], str(qty), Pt(9))

        price = item.get('unit_price', 0)
        set_cell_text(row.cells[6], f'{price:.2f}', Pt(9))

        amount = item.get('amount', 0)
        tax = item.get('tax', 0)
        inv_no = item.get('invoice_no', '')
        if item_count_per_invoice.get(inv_no, 1) > 1:
            display_amount = amount + tax
        else:
            display_amount = item.get('invoice_total', amount + tax)
        set_cell_text(row.cells[7], f'{display_amount:.2f}', Pt(9))

        set_cell_text(row.cells[8], '', Pt(9))

        try:
            print('  {}: {} ({}), qty={}, price={:.2f}, amount={:.2f}'.format(i+1, product, spec, qty, price, display_amount))
        except UnicodeEncodeError:
            pass

    # 清空模板中未使用的数据行
    for i in range(len(all_items), _TOTAL_BASE - 1):
        row = table.rows[i + 1]
        for cell in row.cells:
            set_cell_text(cell, '', Pt(9))

    # 合计行：cells[0~6]合并写大写金额，cells[7]写小写数字，cells[8]备注留空
    total_row = table.rows[total_idx]
    set_cell_text(total_row.cells[0], f'金额合计（大写）：{chinese}', Pt(10.5), bold=True)
    set_cell_text(total_row.cells[7], f'{grand_total:.2f}', Pt(9), bold=True)

    # 签名行：保持签名栏，清空第0列
    sig_row = table.rows[total_idx + 1]
    for p in sig_row.cells[0].paragraphs:
        for run in p.runs:
            run.text = ''

    doc.save(args.output)
    print(f'\nSaved to: {args.output}')

    # 清理temp目录
    temp_dir = os.path.dirname(os.path.abspath(args.data))
    if os.path.basename(temp_dir) == 'temp' and os.path.isdir(temp_dir):
        try:
            shutil.rmtree(temp_dir)
            print(f'已清理临时目录: {temp_dir}')
        except Exception as e:
            print(f'清理temp目录失败: {e}')


if __name__ == '__main__':
    main()
