"""初次导入的本地规则分类；人工分类由台账保存，不在扫描时重算。"""
from pathlib import Path
import re


def _compact(value):
    return re.sub(r'\s+', '', value or '')


def _item_categories(value):
    """只使用商品信息，不把购销方名称或备注当作报销用途。"""
    raw_value = value or ''
    value = _compact(value)
    categories = set()
    if re.search(r'住宿费|住宿服务|房费|客房费|客房服务|酒店住宿|宾馆住宿', value):
        categories.add('住宿费')
    if re.search(r'旅客运输|客运服务|出租汽车|出租车(?:客运|服务|发票)|网约车(?:客运|服务|费)|打车费|乘车费|机票|航空客票|通行费|停车费|过路费|过桥费', value):
        categories.add('出行交通')
    if (re.search(r'试剂|实验(?:仪器|设备|耗材|材料|用品)|科研材料|仪器仪表|电子元器件|电子器件|电子元件|集成电路|电线电缆|计算机(?:配套产品|及配件|配件|外[围部]设备)|金属制品|五金|风动电动工具|电路板|单片机|开发板|传感器|连接器|摄像头|3D打印|打印耗材|杜邦线|铝型材|USB网卡|HDMI', value, re.I)
            or re.search(r'(?<![A-Za-z])PLA(?![A-Za-z])', raw_value, re.I)):
        categories.add('科研材料')
    if re.search(r'餐饮服务|餐费|餐饮费|食品|饮料', value):
        categories.add('其他票据')
    return categories


def classify_invoice(source='', text='', title='', filename=''):
    # 明确票种优先，不因放在普通/工具等目录而误分。
    headings = [_compact(line) for line in text.splitlines() if line.strip()][:12]
    if any(re.match(r'^(?:电子发票[（(]铁路(?:电子)?客票[）)]|铁路(?:电子)?客票(?:$|[（(:：]))', line) for line in headings):
        return '铁路客票'

    items = re.findall(r'^\s*\*([^*\n]+)\*([^\n]+)', text, re.M)
    evidence = [category + '*' + product for category, product in items]
    if not evidence and ' · ' in title:
        evidence = [title]
    categories = set().union(*(_item_categories(item) for item in evidence)) if evidence else set()
    if categories:
        # 跨多个类型的混合发票交给人工判断，保持现有五种分类。
        return next(iter(categories)) if len(categories) == 1 else '其他票据'

    # 图片或文字未提取成功时，只采用文件名中明确的票种提示。
    name = Path(filename).stem
    if re.search(r'铁路(?:电子)?客票|高铁票|动车票|火车票', _compact(name)):
        return '铁路客票'
    categories = _item_categories(name)
    if re.search(r'出租车|网约车|打车', _compact(name)):
        categories.add('出行交通')
    if categories:
        return next(iter(categories)) if len(categories) == 1 else '其他票据'

    # 保留已有目录提示，供没有明确内容的票据使用。
    if '高铁' in source or '铁路' in source:
        return '铁路客票'
    if '车票' in source:
        return '出行交通'
    if '工具' in source:
        return '科研材料'
    return '其他票据'
