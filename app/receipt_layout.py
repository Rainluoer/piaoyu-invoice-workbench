"""修正工作台入库单的固定版式，保留其他 OOXML 部件。"""
from pathlib import Path
from copy import deepcopy
import os
from zipfile import ZipFile

from lxml import etree

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W = '{' + NS['w'] + '}'


def _signature_paragraph(document):
    paragraphs = document.xpath(
        'w:body/w:tbl[2]/w:tr[last()]/w:tc[2][w:tcPr/w:gridSpan/@w:val="8"]/w:p',
        namespaces=NS,
    )
    return paragraphs[0] if len(paragraphs) == 1 else None


def _signature_text(paragraph):
    return ''.join(''.join(paragraph.xpath('.//w:t/text()', namespaces=NS)).split())


def clean_receipt(source, target=None, blank_rows_from=None):
    source = Path(source)
    target = Path(target) if target else source
    original_times = source.stat() if source == target else None
    with ZipFile(source) as package:
        entries = [(info, package.read(info.filename)) for info in package.infolist()]
    xml = next(data for info, data in entries if info.filename == 'word/document.xml')
    document = etree.fromstring(xml)
    body = document.find('w:body', NS)
    changed = False
    header = body.find('w:tbl', NS)
    if header is not None:
        for row in list(header.findall('w:tr', NS))[1:]:
            text = ''.join(row.xpath('.//w:t/text()', namespaces=NS)).replace(' ', '').strip()
            if text in {'XXIJ49项目研发', 'PRJ-VSM23YS6'}:
                # 保留项目名称和代码各自的一行空段及原来的行高。
                for value in row.xpath('.//w:t', namespaces=NS):
                    value.text = ''
                for line_break in row.xpath('.//w:br', namespaces=NS):
                    line_break.getparent().remove(line_break)
                changed = True
    restore_header = header is not None and len(header.findall('w:tr', NS)) == 1
    signature = _signature_paragraph(document)
    # 仅替换旧的空白签名栏，保留已填写的签字或插入的签名对象。
    restore_signature = (signature is not None and _signature_text(signature) == '采购：验收：保管：'
                         and all(child.tag in {W + 'pPr', W + 'r'} for child in signature)
                         and all(child.tag in {W + 'rPr', W + 't', W + 'tab'}
                                 for child in signature.findall('w:r/*', NS)))
    if blank_rows_from and (restore_header or restore_signature):
        with ZipFile(blank_rows_from) as template:
            reference = etree.fromstring(template.read('word/document.xml'))
        if restore_header:
            # 兼容之前已删掉这两行的工作台文件，恢复为模板里的两个空行。
            reference_header = reference.find('w:body/w:tbl', NS)
            for row in reference_header.findall('w:tr', NS)[1:3]:
                header.append(deepcopy(row))
                changed = True
        if restore_signature:
            reference_signature = _signature_paragraph(reference)
            if reference_signature is None or _signature_text(reference_signature) != '采购：验收：保管：审核：':
                raise ValueError('入库单模板缺少完整的签名栏')
            signature[:] = [deepcopy(child) for child in reference_signature]
            changed = True
    # 尾部的空白大字号段落会被 Word 排到新页；不触碰正文、图形或分节。
    for element in reversed(list(body)):
        if element.tag == W + 'sectPr':
            continue
        if element.tag != W + 'p':
            break
        text = ''.join(element.xpath('.//w:t/text()', namespaces=NS)).strip()
        content = element.xpath('.//w:drawing|.//w:pict|.//w:object|.//w:sectPr|.//w:fldChar|.//w:instrText', namespaces=NS)
        if text or content:
            break
        body.remove(element)
        changed = True
    if not changed and source == target:
        return False
    replacement = etree.tostring(document, xml_declaration=True, encoding='UTF-8', standalone=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + '.layout-tmp')
    try:
        with ZipFile(temporary, 'w') as package:
            for info, data in entries:
                package.writestr(info, replacement if info.filename == 'word/document.xml' else data)
        if original_times:
            # 既有下载文件名使用成品时间，版式迁移保留该日期。
            os.utime(temporary, ns=(original_times.st_atime_ns, original_times.st_mtime_ns))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return changed


def blank_receipt_fields(source, blank_cells, blank_total=False, product_labels=None):
    """清除原模板脚本的默认占位值，保留用户未填写的空白及原有版式。"""
    source = Path(source)
    with ZipFile(source) as package:
        entries = [(info, package.read(info.filename)) for info in package.infolist()]
    document = etree.fromstring(next(data for info, data in entries if info.filename == 'word/document.xml'))
    table = document.findall('w:body/w:tbl', NS)[1]
    rows = table.findall('w:tr', NS)

    def cells(row):
        result = []
        for cell in row.findall('w:tc', NS):
            span = cell.find('w:tcPr/w:gridSpan', NS)
            result.extend([cell] * (int(span.get(W+'val')) if span is not None else 1))
        return result

    def clear(cell, label=''):
        texts = cell.findall('.//w:t', NS)
        for index, text in enumerate(texts):
            text.text = label if index == 0 else ''

    for index, (row, columns) in enumerate(zip(rows[1:], blank_cells)):
        logical_cells = cells(row)
        for column in columns:
            clear(logical_cells[column])
        if product_labels and product_labels[index] is not None:
            clear(logical_cells[2], product_labels[index])
    if blank_total:
        total_cells = cells(rows[max(8, len(blank_cells)+1)])
        clear(total_cells[0], '金额合计（大写）：')
        clear(total_cells[7])
    replacement = etree.tostring(document,xml_declaration=True,encoding='UTF-8',standalone=True)
    temporary = source.with_suffix(source.suffix+'.blank-tmp')
    try:
        with ZipFile(temporary,'w') as package:
            for info, data in entries:
                package.writestr(info,replacement if info.filename == 'word/document.xml' else data)
        os.replace(temporary,source)
    finally:
        temporary.unlink(missing_ok=True)
