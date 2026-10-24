"""按页面坐标重建横排文字；排除旋转的侧边水印，保留数字之间的真实间距。"""
from statistics import median


def layout_lines(page):
    rows = []
    seen = set()
    for char in sorted(page.chars, key=lambda c: (c['top'], c['x0'])):
        text = char.get('text', '')
        if not text.strip() or not char.get('upright', True):
            continue
        a, b, c, d, _, _ = char.get('matrix', (1, 0, 0, 1, 0, 0))
        if a <= 0 or d <= 0 or abs(b) > .01 or abs(c) > .01:
            continue
        key = (text, round(char['x0'], 1), round(char['top'], 1))
        if key in seen:
            continue
        seen.add(key)
        center = (char['top'] + char['bottom']) / 2
        height = char['bottom'] - char['top']
        if rows and abs(center - median(r['center'] for r in rows[-1])) <= max(2, min(height * .3, 3)):
            rows[-1].append({**char, 'center': center})
        else:
            rows.append([{**char, 'center': center}])
    result = []
    for chars in rows:
        chars.sort(key=lambda c: c['x0'])
        text = ''
        previous = None
        for char in chars:
            if previous is not None:
                gap = char['x0'] - previous['x1']
                if gap > 1:
                    text += ' '
            text += char['text']
            previous = char
        result.append(dict(text=text, chars=chars, top=min(c['top'] for c in chars), bottom=max(c['bottom'] for c in chars)))
    return result


def layout_text(page):
    return '\n'.join(row['text'] for row in layout_lines(page))
