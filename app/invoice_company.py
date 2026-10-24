"""识别发票销售方名称，支持购销并排、竖排标签及独立 PDF 文字层。"""
import re
import threading

PDFIUM_LOCK = threading.RLock()
_NAME = r'名[ \t\u3000]*称'
_STOP = r'统一社会信用代码|纳税人识别号|地址[、，]?电话|开户行及账号|开户银行|银行账号|购买方(?:名称)?'


def company_needs_repair(value):
    return bool(re.search(r'(?:下\s*)?载\s*次\s*数|统一社会信用代码|纳税人识别号|购买方名称', value or ''))


def _clean_name(value):
    value = re.split(_STOP, value, maxsplit=1)[0]
    value = re.sub(r'^\s*' + _NAME + r'\s*[:：]?\s*', '', value)
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', value)
    value = re.sub(r'\s+', ' ', value).strip(' :：;；')
    if (not 2 <= len(value) <= 200 or company_needs_repair(value) or value in {'名称', '未填写', '待填写', '无'}
            or re.fullmatch(r'[A-Z0-9]{15,20}', value) or not re.search(r'[A-Za-z\u4e00-\u9fff]', value)):
        return ''
    return value


def seller_from_text(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        # 标准显式标签。限制在本行或紧邻下一行，不跨到购买方或商品区域。
        match = re.search(r'销\s*售\s*方(?:\s*信\s*息)?\s*(?:' + _NAME + r')?\s*[:：]\s*(.*)', line)
        if match:
            value = match.group(1).strip()
            if not value and index + 1 < len(lines):
                value = lines[index + 1].strip()
            if not re.match(r'购买方|项目名称|发票号码|' + _STOP, value):
                name = _clean_name(value)
                if name:
                    return name
        # 常见票面为“购 名称：买方 销 名称：卖方”或“买 名 称 买方 售 名 称 卖方”。
        names = list(re.finditer(_NAME + r'\s*[:：]?\s*', line))
        if len(names) == 2:
            parallel = bool(re.search(r'[购买]', line[:names[0].start()]) and re.search(r'[销售]', line[names[0].end():names[1].start()]))
            separate = index > 0 and re.sub(r'\s+', '', lines[index-1]) == '购销' and index+1 < len(lines) and re.sub(r'\s+', '', lines[index+1]) == '买售'
            if parallel or separate:
                name = _clean_name(line[names[1].end():])
                if name:
                    return name
    return ''


def _bounded_seller(page, textpage, raw):
    # 文字层顺序可能把值放到所有标签后面，只按同一行的左右“名称”坐标定位。
    compact = re.sub(r'\s+', '', raw)
    if '购买方' not in compact or '销售方' not in compact:
        return ''
    names = []
    for keyword in ('名称', '名 称'):
        search = textpage.search(keyword)
        try:
            found = search.get_next()
            while found:
                index, count = found
                boxes = [textpage.get_charbox(i) for i in range(index, index + count)]
                names.append((min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)))
                found = search.get_next()
        finally:
            search.close()
    for left, bottom, right, top in sorted(names, reverse=True):
        height = max(top - bottom, 5)
        if left < page.get_width() * .48:
            continue
        if not any(other[0] < page.get_width() * .45 and abs(other[1] - bottom) < height for other in names):
            continue
        value = textpage.get_text_bounded(left, max(0, bottom - height * 1.25), page.get_width(), top + height * .3)
        name = _clean_name(value)
        if name:
            return name
    return ''


def company_from_pdf(path):
    try:
        import pdfplumber
        from pdf_layout import layout_text
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages[:2]:
                company = seller_from_text(layout_text(page))
                if company:
                    return company
    except Exception:
        pass
    try:
        import pypdfium2 as pdfium
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                for index in range(min(len(pdf), 2)):
                    page = pdf[index]
                    try:
                        textpage = page.get_textpage()
                        try:
                            raw = textpage.get_text_range()
                            name = seller_from_text(raw) or _bounded_seller(page, textpage, raw)
                            if name:
                                return name
                        finally:
                            textpage.close()
                    finally:
                        page.close()
            finally:
                pdf.close()
    except Exception:
        # 图片、损坏文件或缺少依赖仍允许手工补填，不用购买方或任意公司凑数。
        pass
    return ''
