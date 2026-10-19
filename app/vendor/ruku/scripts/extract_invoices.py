import subprocess
import sys
import importlib

_REQUIRED_PACKAGES = {
    'pdfplumber': 'pdfplumber',
}


def _ensure_deps():
    for mod_name, pkg_name in _REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(mod_name)
        except ImportError:
            print(f'正在安装依赖 {pkg_name}...')
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg_name, '-q'])


_ensure_deps()

import pdfplumber
import re
import os
import json
import argparse

_SPEC_CONT_PATTERN = re.compile(r'[】\]】]|^质[\/\-]|^材[质]')

# 单位词汇（先双字后单字，re.escape + | 拼接）
_UNIT_TOKENS = [
    # 双字单位
    '套件', '平方米', '立方毫米', '立方厘米', '立方米',
    '毫安', '毫伏', '毫升', '千瓦', '千克',
    '安培', '伏特', '瓦特', '赫兹', '摩尔', '流明', '勒克斯',
    '公斤', '台套',
    # 单字单位
    '个', '份', '件', '台', '套', '箱', '只', '支', '根', '条',
    '张', '块', '片', '袋', '包', '瓶', '盒', '罐', '桶', '卷',
    '米', '千', '克', '吨',
    '双', '对', '副', '付', '粒', '枚', '批', '组', '架',
    '辆', '艘', '头', '匹', '亩', '本', '页', '间', '座',
    '处', '部', '把', '扇', '顶', '面', '口', '座',
]
_UNITS_PATTERN = '|'.join(re.escape(t) for t in _UNIT_TOKENS)


def _is_spec_continuation(text):
    return bool(_SPEC_CONT_PATTERN.search(text))


_PROD_TYPES = re.compile(
    '|'.join(['螺丝', '螺钉', '螺帽', '螺栓', '螺母', '垫圈', '垫片', '华司', '弹簧',
              '交换机', '无线AP', '电池盒', '电子元件'])
)
_REDUNDANT_WORDS = ['大全', '介子', '机螺']


def _shorten_product(name, category='', max_len=50):
    """智能缩短过长的品名，根据内容缩词而非简单截断"""
    # 预留类别前缀空间
    prefix_len = len(category) + 2 if category else 0
    effective_max = max(max_len - prefix_len, 15) if prefix_len else max_len

    # 1. 去掉末尾纯数字型号串
    name = re.sub(r'(?:[A-Z]?\d+[A-Z]?)+$', '', name).rstrip()

    # 2. 压缩型号范围
    def _cm(m):
        nums = re.findall(r'\d+', m.group())
        return f'{nums[0]}-{nums[-1]}等' if len(nums) >= 3 else m.group()
    name = re.sub(r'(?:[A-Z]\d+){3,}', _cm, name)

    # 3. 合并连续的同义产品类型词
    matches = list(_PROD_TYPES.finditer(name))
    if len(matches) >= 2:
        first = matches[0]
        last = matches[-1]
        span_text = name[first.start():last.end()]
        span_types = sum(len(m.group()) for m in _PROD_TYPES.finditer(span_text))
        if len(span_text) > 0 and span_types / len(span_text) > 0.3:
            candidate = name[:first.end()] + name[last.start():]
            if len(candidate) < len(name):
                name = candidate

    # 4. 去除冗余词
    for rw in _REDUNDANT_WORDS:
        idx = name.find(rw)
        if idx >= 0:
            name = name[:idx] + name[idx + len(rw):]

    if len(name) <= effective_max:
        return name

    # 5. 截断到最后一个中文字符后加"..."
    chinese_end = re.search(r'[\u4e00-\u9fff]', name[::-1])
    if chinese_end:
        cut = len(name) - chinese_end.start()
        if cut > effective_max - 3:
            cut = effective_max - 3
        return name[:cut] + '...'
    return name[:effective_max - 3] + '...'


def extract_invoice_info(pdf_path):
    """从PDF发票中提取信息，使用pdfplumber原始文本 + 改进正则"""
    has_text = False
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            has_text = True

            info = {}

            # 发票号码：优先标签同行匹配，失败则兜底搜索 20 位号码
            match = re.search(r'发票号码[：:]\s*(\d{6,20})', text)
            if not match:
                match = re.search(r'(?<!\d)(\d{20})(?!\d)', text)
            if match:
                info['invoice_no'] = match.group(1)

            # 开票日期：优先标签同行匹配，失败则兜底搜索日期格式
            match = re.search(r'开票日期[：:]\s*(\d{4}年\d{2}月\d{2}日)', text)
            if not match:
                match = re.search(r'(\d{4}年\d{2}月\d{2}日)', text)
            if match:
                info['date'] = match.group(1)

            match = re.search(r'小写[）\)]\s*[¥￥]?\s*([\d,.]+)', text)
            if match:
                info['total'] = float(match.group(1).replace(',', ''))

            lines = text.split('\n')
            items = []

            for i, line in enumerate(lines):
                if not line.startswith('*'):
                    continue

                # 格式A：含单位（科研材料发票）——*类别*品名 规格 单位 数量 单价 金额 税率 税额
                m = re.search(
                    r'\*([^*\n]+)\*\s*(.+)\s+(' + _UNITS_PATTERN + r')\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+(\d+%)\s+([\d.]+)\s*$',
                    line
                )
                if m:
                    category = m.group(1).strip()
                    product_spec = m.group(2).strip()
                    unit = m.group(3)
                    quantity = int(m.group(4))
                    unit_price = float(m.group(5))
                    amount = float(m.group(6))
                    tax_rate = m.group(7)
                    tax = float(m.group(8))

                    parts = product_spec.split(None, 1)
                    product_truncated = parts[0]
                    spec = parts[1] if len(parts) > 1 else ''
                else:
                    # 格式B：无单位（打车电子发票）——*类别*品名 单价 数量 金额 税率 税额
                    m = re.search(
                        r'\*([^*\n]+)\*\s*(.+?)\s+([\d.]+)\s+(\d+)\s+([\d.]+)\s+(\d+%)\s+([\d.]+)\s*$',
                        line
                    )
                    if not m:
                        continue
                    category = m.group(1).strip()
                    product_truncated = m.group(2).strip()
                    spec = ''
                    unit = ''
                    unit_price = float(m.group(3))
                    quantity = int(m.group(4))
                    amount = float(m.group(5))
                    tax_rate = m.group(6)
                    tax = float(m.group(7))

                full_product = product_truncated
                for j in range(i + 1, min(i + 5, len(lines))):
                    nl = lines[j].strip()
                    if not nl or nl.startswith('*'):
                        break
                    nl_clean = nl.replace(' ', '')
                    if '合计' in nl_clean or '价税' in nl_clean:
                        break
                    # 金额/税额行（如「¥31.07 ¥0.93」）不是品名续文，停止拼接
                    if nl.startswith('¥') or nl.startswith('￥'):
                        break

                    # 续行中可能包含规格续文 (空格后的部分)
                    if ' ' in nl:
                        left, right = nl.split(' ', 1)
                        right = right.strip()
                        if right and _is_spec_continuation(right):
                            spec += right
                            full_product += left
                            continue

                    full_product += nl

                full_product = re.sub(r'\s+', '', full_product)
                full_product = _shorten_product(full_product, category)

                items.append({
                    'category': category,
                    'product': full_product,
                    'spec': spec,
                    'unit': unit if unit else '个',
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'amount': amount,
                    'tax_rate': tax_rate,
                    'tax': tax
                })

            if items:
                info['items'] = items

            # 只要提取到任一关键信息就返回，避免因单一字段缺失而丢弃整张发票
            if info.get('invoice_no') or info.get('total') is not None or info.get('items'):
                return info
            return None
    if not has_text:
        print(f'  [提示] {os.path.basename(pdf_path)}: 无文本层（可能为扫描件/图片），需 OCR 识别')
    return None


def main():
    parser = argparse.ArgumentParser(description='提取发票PDF中的商品信息（pdfplumber版）')
    parser.add_argument('--invoice_dir', required=True, help='发票PDF所在目录')
    parser.add_argument('--output', required=True, help='输出JSON文件路径')
    args = parser.parse_args()

    results = []

    for filename in sorted(os.listdir(args.invoice_dir)):
        if filename.endswith('.pdf'):
            pdf_path = os.path.join(args.invoice_dir, filename)
            info = extract_invoice_info(pdf_path)
            if info:
                info['original_filename'] = filename
                results.append(info)
                items = info.get('items', [])
                print(f'  {filename}: {len(items)} items, total={info.get("total", "N/A")}')
                for it in items:
                    p = it.get('product', '')
                    s = it.get('spec', '')
                    print(f'    品名={p} (规格={s})')
            else:
                print(f'  {filename}: FAILED')

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f'\nTotal: {len(results)} invoices extracted')
    print(f'Saved to: {args.output}')


if __name__ == '__main__':
    main()
