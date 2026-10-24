"""原提取失败或数字校验失败时，按 PDF 列位置重建科研材料明细。"""
from decimal import Decimal, InvalidOperation
import re

from pdf_layout import layout_lines

NUMBER = r'-?\d+(?:\.\d+)?'
TAIL = re.compile(r'\s+([A-Za-z\u4e00-\u9fff]{1,8})\s+(' + NUMBER + r')\s+(' + NUMBER + r')\s+(' + NUMBER + r')\s+(\d+(?:\.\d+)?%|免税|不征税)\s+(' + NUMBER + r')\s*$')
DISCOUNT_TAIL = re.compile(r'\s+(-\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?%)\s+(' + NUMBER + r')\s*$')


class ExtractionRepairError(ValueError):
    """整批仍有疑点时携带各张成功结果，避免前一张失败丢掉后续修复。"""
    def __init__(self, invoices, repaired, errors):
        super().__init__('；'.join(errors) + '；可补填后继续生成')
        self.invoices, self.repaired, self.errors = invoices, repaired, errors


def _decimal(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > 100000000:
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('商品数字字段无效') from None


def check_invoice(info, strict_total=True):
    if not isinstance(info, dict) or not info.get('invoice_no') or info.get('total') is None or not isinstance(info.get('items'), list) or not info['items']:
        raise ValueError('票号、商品或价税合计未完整识别')
    total = _decimal(info['total'])
    if total < 0:
        raise ValueError('暂不自动生成负数发票')
    amounts = Decimal(0)
    for item in info['items']:
        if not isinstance(item, dict) or not isinstance(item.get('product'), str) or not item['product'].strip():
            raise ValueError('商品名称未识别')
        quantity, price, amount, tax = [_decimal(item.get(key)) for key in ('quantity','unit_price','amount','tax')]
        if quantity <= 0 or price < 0 or amount < 0 or tax < 0:
            raise ValueError('商品数量或金额异常')
        if abs(quantity * price - amount) > Decimal('.015'):
            raise ValueError('数量乘单价与不含税金额不符，请核对数字列')
        amounts += amount + tax
    discounts = info.get('discounts', [])
    if not isinstance(discounts, list):
        raise ValueError('折扣行格式无效')
    for discount in discounts:
        # 只自动归属完整品名、类目相同的单商品折扣，其他情况继续补填。
        if (len(info['items']) != 1 or not isinstance(discount, dict)
                or discount.get('product') != info['items'][0]['product']
                or discount.get('category') != info['items'][0].get('category')
                or discount.get('tax_rate') != info['items'][0].get('tax_rate')):
            raise ValueError('折扣归属不明确，请核对商品')
        amount, tax = _decimal(discount.get('amount')), _decimal(discount.get('tax'))
        if amount >= 0 or tax > 0:
            raise ValueError('折扣金额或税额无效')
        amounts += amount + tax
    if amounts < 0:
        raise ValueError('折扣超过商品金额')
    # 未归属的折扣或漏项交给补填；不凭整单金额推算并伪造商品数字。
    if (strict_total or len(info['items']) > 1) and abs(amounts - total) > Decimal('.01'):
        raise ValueError('商品含税金额与价税合计不符，可能有漏项或折扣行')
    return info


def _column_start(row, label):
    chars = row['chars']
    text = ''.join(c['text'] for c in chars)
    index = text.find(label)
    if index < 0:
        raise ValueError('未定位到完整的商品表头')
    return chars[index]['x0'] - 3


def _cell(row, left, right):
    return ''.join(c['text'] for c in row['chars'] if left <= (c['x0'] + c['x1']) / 2 < right).strip()


def from_pages(pages):
    result = {'items': []}
    numbers, dates, totals = set(), set(), set()
    for rows in pages:
        text = '\n'.join(row['text'] for row in rows)
        flat = re.sub(r'[ \t\u3000]', '', text)
        numbers.update(re.findall(r'发票号码[:：]?(\d{8,30})', flat))
        dates.update(re.findall(r'开票日期[:：]?(\d{4}年\d{1,2}月\d{1,2}日)', flat))
        totals.update(re.findall(r'小写[）)][:：]?[¥￥]?([\d,]+\.\d{2})', flat))
        header = None
        current = None
        for row in rows:
            compact = re.sub(r'\s+', '', row['text'])
            if all(label in compact for label in ('项目名称', '规格型号', '单位', '数量', '单价', '金额', '税额')):
                header = row
                continue
            if header is None:
                continue
            if compact.startswith('合计') or '价税合计' in compact:
                header = None
                continue
            spec_left = _column_start(header, '规格型号')
            unit_left = _column_start(header, '单位')
            if compact.startswith('*'):
                match = TAIL.search(row['text'])
                discount = DISCOUNT_TAIL.search(row['text']) if not match else None
                if not match and not discount:
                    raise ValueError('存在无法可靠拆分的商品行，请手动核对')
                name = _cell(row, 0, spec_left)
                category = re.fullmatch(r'\*([^*]+)\*(.*)', name)
                if not category:
                    raise ValueError('商品名称与规格列边界不明确')
                if discount:
                    # 数量、单价、单位必须实际为空，不能把错位的负数字段当折扣。
                    amount_left = _column_start(header, '金额')
                    if _cell(row, unit_left, amount_left):
                        raise ValueError('折扣行的数量或单价列非空')
                    amount, rate, tax = discount.groups()
                    current = dict(category=category[1], product=category[2], amount=float(amount), tax_rate=rate, tax=float(tax), spec=_cell(row,spec_left,unit_left))
                    result.setdefault('discounts', []).append(current)
                else:
                    unit, quantity, price, amount, rate, tax = match.groups()
                    current = dict(category=category[1], product=category[2], spec=_cell(row,spec_left,unit_left),
                        unit=unit, quantity=float(quantity), unit_price=float(price), amount=float(amount), tax_rate=rate, tax=float(tax))
                    if current['quantity'].is_integer():
                        current['quantity'] = int(current['quantity'])
                    result['items'].append(current)
            elif current:
                # 续行只读取品名及规格列；有额外金额的折扣/未识别商品不能并入品名。
                if re.search(r'\d+\.\d{2}', _cell(row, unit_left, 100000)):
                    raise ValueError('发现未归属的金额行，请手动核对折扣或漏项')
                current['product'] += _cell(row, 0, spec_left)
                current['spec'] += _cell(row, spec_left, unit_left)
    if len(numbers) != 1 or len(totals) != 1 or len(dates) > 1:
        raise ValueError('票号或价税合计存在缺失、冲突')
    result.update(invoice_no=numbers.pop(), total=float(totals.pop().replace(',','')))
    if dates:
        result['date'] = dates.pop()
    return check_invoice(result, strict_total=True)


def extract_layout_invoice(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return from_pages([layout_lines(page) for page in pdf.pages[:8]])


def repair_extraction(invoices, invoice_dir, filenames):
    """保留原脚本的有效结果；有疑点时整张回退，禁止拼接来自不同票据的字段。"""
    if not isinstance(invoices, list):
        raise ValueError('提取结果格式无效')
    by_name = {}
    for record in invoices:
        if not isinstance(record, dict) or record.get('original_filename') not in filenames or record.get('original_filename') in by_name:
            raise ValueError('提取结果的发票来源重复或无效')
        by_name[record.get('original_filename')] = record
    repaired, changed, errors = [], [], []
    for filename in filenames:
        record = {key: value for key, value in by_name.get(filename, {}).items() if key != '_extraction_error'}
        try:
            check_invoice(record)
        except (ValueError, KeyError):
            try:
                record = extract_layout_invoice(invoice_dir / filename)
            except Exception as error:
                message = f'{filename} 自动识别未通过校验：{error}'
                errors.append(message)
                record = {**record, 'original_filename': filename, '_extraction_error': message}
            else:
                record['original_filename'] = filename
                changed.append(filename)
        repaired.append(record)
    if errors:
        raise ExtractionRepairError(repaired, changed, errors)
    return repaired, changed
