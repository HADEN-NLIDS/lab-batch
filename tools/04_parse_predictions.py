# -*- coding: utf-8 -*-
"""04 本地：解析 OCR artifacts → predictions.jsonl（无需GPU，可反复迭代）

设计要点（踩坑后定型，改动前先看记录）：
  1. 数据来源用 parsing_res_list，不是 markdown（当前 artifacts 未保存 markdown 字段）
  2. 放弃列索引，改用类型识别：真实报告有双栏排版，一行左右两栏，
     表头格数与数据格数对不上，列映射必然错位
  3. item_name 优先用中文名（code 只作兜底）——MedRepBench 金标准 94% 用中文名，
     自采报告也期望中文名；'项目|缩写|结果' 这类结构若优先用 code 会得到 TP 而非 总蛋白
  4. 单位正则必须覆盖 10E9/L、10^12/L、×10^9/L 三种写法；classify() 里单位判定
     必须排在 value_glued 之前，否则 10^9/L 会被当成"值10+单位^9/L"
  5. 需过滤：整行合并的元信息、说明文字（"不足：12-20"）、表头残片、LaTeX残留

用法：
    python tools\\04_parse_predictions.py --artifacts artifacts --dump 2
    python tools\\04_parse_predictions.py --artifacts artifacts --gold-dir gold
"""

import os
import re
import sys
import json
import glob
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

HEADER_MAP = {
    'item_name': ['项目', '项目名称', '检验项目', '名称', '检查项目', '项目名', '指标', '分析项目'],
    'item_value': ['结果', '检验结果', '测定值', '测量值', '数值', '结果值', '检测结果'],
    'item_unit': ['单位', '计量单位', '单位符号'],
    'item_range': ['参考范围', '参考区间', '参考值', '正常范围', '正常参考值', '范围', '参考'],
}

CN_RE = re.compile(r'[\u4e00-\u9fff]')
CODE_RE = re.compile(r'^[\*A-Za-z][A-Za-z0-9#%\.\+\-]{0,12}$')
NUM_FULL = re.compile(r'^[<>≤≥]?\s*[-+]?\d+(?:\.\d+)?\s*[↑↓]?$')
RANGE_FULL = re.compile(
    r'^[<>≤≥]?\s*[-+]?\d+(?:\.\d+)?\s*(?:-{1,2}|~|～|—|–)\s*[-+]?\d+(?:\.\d+)?')
UNIT_FULL = re.compile(
    r'^('
    r'%'
    r'|(?:×\s?)?10(?:\^?\d+|E\d+)\s*[/／]?\s*[a-zA-Zμ]*'
    r'|[a-zA-Zμ]+\s*[/／]\s*[a-zA-Z0-9]+'
    r'|[a-zA-Zμ]{1,6}'
    r')$'
)

NOISE_KEYS = (
    '姓名', '性别', '科室', '病历号', '送检', '检验日', '床号', '编号',
    '临床印象', '标本', '检验者', '审核者', '诊断', '备注', '合计',
    '分析项目', '项目', '检验项目', '代码', '报告单', '打印', '医院',
    '不足', '适宜', '充足', '缺乏', '参考值：', '考值', 'DOB值', '序号',
    'ID号', '样本号', '条码号',
)

# 水印序号：'黔HR 12'、'浙江省HR'、'全国HR'。
# 这些是报告单底纹/防伪水印被 OCR 认出来的结果，若当成项目名会污染整行。
# HR 前面要求至少 1 个字符，避免把真正的心率单元格 'HR' 误删。
WATERMARK_RE = re.compile(r'^.{1,6}HR\s*\d{0,3}$')

# 报告日期。前后对比 / 引用补全都要靠它，但脱敏常把日期一起涂掉，
# 所以抽不到是正常情况，留空即可，不要因此报错。
DATE_LABEL = (r'(?:检验日期|报告日期|送检日期|送检日|采样时间|采集时间|'
              r'检验时间|上机时间|打印日期)')
DATE_RE = re.compile(
    DATE_LABEL + r'[:：]?\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})')


def extract_report_date(blocks):
    """从版面块里抽报告日期，返回 ISO 日期或空串。"""
    for b in blocks or []:
        txt = b.get('content') or ''
        m = DATE_RE.search(txt)
        if m:
            y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return '%s-%02d-%02d' % (y, mo, d)
    return ''

# 双大写字母比值缩写（'A/G'、'T/C'），不是单位
RATIO_ABBR = re.compile(r'^[A-Z]\s*[/／]\s*[A-Z]$')

# 多字母全大写比值名：'HDL/CHOL'、'HDL/CHL'、'LDL/HDL'。
# 这类会被 UNIT_FULL 的 '字母/字母' 分支吃掉，而它既不是名称也不是代码，
# 整项就被丢掉了（090039 的 HDL/CHOL 就是这么丢的）。
# 真实单位左右不会同时是 2 个以上全大写字母：g/L、U/L、mmol/L、mg/dL、fL 都不匹配。
RATIO_FULL = re.compile(r'^[A-Z]{2,}\s*[/／]\s*[A-Z]{2,}$')

# 单侧参考范围（'≤26'、'>1'、'≥3.9'），数值之后出现时按范围处理
ONE_SIDED_RANGE = re.compile(r'^[<>≤≥＜＞]\s*[-+]?\d+(?:\.\d+)?\s*[↑↓]?$')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', required=True)
    ap.add_argument('--gold-dir', default='')
    ap.add_argument('--out', default='predictions.jsonl')
    ap.add_argument('--dump', type=int, default=0, help='打印前N份结构后退出')
    ap.add_argument('--report', default='parse_report.md')
    return ap.parse_args()


def strip_tags(s):
    s = re.sub(r'<br\s*/?>', ' ', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = s.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    return re.sub(r'\s+', ' ', s).strip()


def clean_cell(s):
    """清理单格：LaTeX箭头/上标、行号前缀、多余空白。"""
    if not s:
        return ''
    # OCR 会把单元格内的换行输出成字面量 '\n'（反斜杠+n 两个字符），
    # 不处理就会得到 '抗核糖核蛋白抗体\n[anti-RNP/Sm]'，与金标准永远对不上。
    # 注意顺序：必须在 LaTeX 清理之前，且只替换精确的"反斜杠+n"，
    # 不会误伤 '\gamma' 这类。
    s = s.replace('\\r\\n', ' ').replace('\\n', ' ').replace('\\t', ' ').replace('\\r', ' ')
    s = re.sub(r'\$\s*\\uparrow\s*\$', '↑', s)
    s = re.sub(r'\$\s*\\downarrow\s*\$', '↓', s)
    # 希腊字母必须先转，否则会被下面的通用 '\xxx' 清理整段吃掉（'$\gamma$' → ''）
    s = re.sub(r'\$\s*\\(gamma|alpha|beta|mu|times)\s*\$',
               lambda m: {'gamma': 'γ', 'alpha': 'α', 'beta': 'β',
                          'mu': 'μ', 'times': '×'}[m.group(1)], s)
    s = re.sub(r'\$\s*\\[a-zA-Z]+\s*\$', '', s)
    s = s.replace('\\uparrow', '↑').replace('\\downarrow', '↓')
    s = s.replace('\\mu', 'μ').replace('\\times', '×')
    s = s.replace('\\gamma', 'γ').replace('\\alpha', 'α').replace('\\beta', 'β')
    # 'μ mol/L' → 'μmol/L'：希腊字母是单个符号，OCR 常在它和后面的字母之间插空格，
    # 单位正则认不出 'μ mol/L'。只处理"希腊字母 + 空格 + 字母"，不动中文。
    s = re.sub(r'(?<=[\u03bc\u03b1\u03b2\u03b3])\s+(?=[A-Za-z])', '', s)
    s = s.replace('$', '')
    # 10^{9}/L -> 10^9/L（单位正则只认 ^9，不认 ^{9}）
    s = re.sub(r'\^\{\s*(\d+)\s*\}', r'^\1', s)
    s = re.sub(r'[{}]', '', s)
    # '1 25羟基维生素D' -> '25羟基维生素D'
    s = re.sub(r'^\s*\d{1,2}\s+(?=\d*[\u4e00-\u9fff])', '', s)
    # '(1) 颈管细胞'、'(2) 霉菌' —— TCT/病理报告的勾选序号，不属于项目名。
    # 注意 '(1) 25(OH)D' 这类是真实化学名的一部分，所以要求括号后紧跟中文或字母。
    s = re.sub(r'^\s*[(（]\s*\d{1,2}\s*[)）]\s*(?=[\u4e00-\u9fffA-Za-z])', '', s)
    # 报告单用 '*' 标记自费/医保项目，'★' 标记重点项目，都不属于名称
    s = re.sub(r'^\s*[\*＊★☆]\s*', '', s)
    # 括号统一成半角：OCR 常把 '(WBC)' 认成全角 '（WBC）'，
    # 而 MedRepBench 官方口径的 normalize_text 不处理括号宽度，保持全角会导致名称匹配全错。
    s = s.replace('（', '(').replace('）', ')')
    return re.sub(r'\s+', ' ', s).strip()


def is_noise_label(label):
    """元信息行、说明文字、表头残片一律剔除。"""
    if not label:
        return True
    s = label.replace(' ', '')
    for k in NOISE_KEYS:
        if s.startswith(k) or (k in s and len(s) <= len(k) + 6):
            return True
    if '：' in s and ('<' in s or '>' in s or '≥' in s or '≤' in s):
        return True
    # 含空格分隔的多字段元信息行：'ID号: 2401051169 病历号: 送检医生: 备注:'。
    # 判据是"有空格 + 至少两个冒号"。不能只看冒号个数——'ApoA1:ApoB(ApoA1:ApoB)'
    # 这类比值名也有两个冒号，但它没有空格。
    if (' ' in label) and (label.count(':') + label.count('：')) >= 2:
        return True
    return False


def parse_block(text):
    """解析 parsing_res_list 的单个 block：label / bbox / content。"""
    m = re.search(r'label:\s*(\S+)', text)
    label = m.group(1) if m else ''
    m = re.search(r'bbox:\s*(\[[^\]]*\])', text)
    bbox = m.group(1) if m else ''
    m = re.search(r'content:\s*(.*)', text, re.S)
    content = m.group(1).strip() if m else ''
    return {'label': label, 'bbox': bbox, 'content': content}


def extract_blocks(rec):
    blocks = []
    for res in (rec.get('results') or []):
        if not isinstance(res, dict):
            continue
        for s in (res.get('parsing_res_list') or []):
            if not isinstance(s, str):
                continue
            for part in s.split('#################'):
                if 'label:' in part:
                    blocks.append(parse_block(part))
    return blocks


def get_markdown_text(rec):
    """兼容路径：当前 artifacts 未保存该字段，保留以便日后扩展。"""
    texts = []

    def walk(o, depth=0):
        if depth > 8:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k == 'markdown_texts' and isinstance(v, str):
                    texts.append(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for x in o:
                walk(x, depth + 1)

    walk(rec.get('results'))
    return texts


def get_html_sources(rec):
    htmls = []
    for b in extract_blocks(rec):
        c = b.get('content') or ''
        if c and '<table' in c.lower():
            htmls.append(c)
    if not htmls:
        for t in get_markdown_text(rec):
            if '<table' in t.lower():
                htmls.append(t)
    return htmls


def extract_tables(html_texts):
    tables = []
    blob = '\n'.join(html_texts)
    for t in re.findall(r'<table[^>]*>(.*?)</table>', blob, re.S | re.I):
        rows = []
        for r in re.findall(r'<tr[^>]*>(.*?)</tr>', t, re.S | re.I):
            cells = [strip_tags(c) for c in
                     re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.S | re.I)]
            if any(c for c in cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def classify(cell, prev_is_num=False):
    """按内容分类。prev_is_num 用于区分 fL 这类既像代码又像单位的串。"""
    c = (cell or '').strip()
    if not c:
        return 'empty'
    if RANGE_FULL.match(c):
        return 'range'
    inner = c.strip('()（）【】[] ')
    if inner != c and RANGE_FULL.match(inner):
        return 'range'
    if NUM_FULL.fullmatch(c):
        return 'value'
    # 全大写多字母比值名必须排在单位判断之前，否则会被当成单位、整项丢弃
    if RATIO_FULL.match(c):
        return 'name'
    # 单位必须排在 value_glued 之前：否则 10^9/L 会被拆成"值10 + 单位^9/L"
    if not CN_RE.search(c) and len(c) <= 14 and UNIT_FULL.match(c):
        return 'unit'
    # 数字开头、末尾只粘了少量中文（正常/偏高/↓这类状态词）→ 仍是值格
    m = NUM_PREFIX.match(c)
    if m and len(CN_RE.findall(m.group(2) or '')) <= 3 and not CN_RE.search(m.group(1)):
        return 'value_glued'
    if CN_RE.search(c):
        return 'name'
    # 比值/带括号的拉丁项目名：'ApoA1:ApoB(ApoA1:ApoB)'、'AST:ALT(AST:ALT)'、'PT(%)'。
    # 这类名字含冒号或括号，CODE_RE 匹配不上，以前会被当 'other' 直接丢掉，
    # 结果是整份报告里所有比值项都漏抽。
    if LATIN_NAME_RE.match(c) and not UNIT_FULL.match(c.replace(' ', '')):
        return 'name'
    if prev_is_num and UNIT_FULL.match(c):
        return 'unit'
    if CODE_RE.fullmatch(c):
        return 'code'
    if UNIT_FULL.match(c) and len(c) <= 10:
        return 'unit'
    return 'other'


NUM_PREFIX = re.compile(r'^([<>≤≥]?\s*[-+]?\d+(?:\.\d+)?)\s*(.*)$')

# 纯拉丁的项目名，且带冒号或括号（比值项、带说明的缩写）
LATIN_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9#%\.\+\-]*[:：(（][^/／]{0,30}$')


def split_num_prefix(c):
    """从粘在一起的格子里拆出数值与前缀：'7.1 ( 10^9' -> ('7.1','( 10^9')。"""
    m = NUM_PREFIX.match((c or '').strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, (c or '').strip()


def build_item(cells):
    """组内按类型取值。名称优先用中文名，code 兜底。"""
    code = name = value = unit = rng = ''
    name_idx = -1
    val_cands = []          # [(idx, text)]，最后再决定哪个是结果
    rng_cands = []          # 带 <、>、≤、≥ 但尾巴粘了单位的单侧范围
    for idx, c in enumerate(cells):
        prev_is_num = idx > 0 and bool(NUM_FULL.fullmatch(cells[idx - 1] or ''))
        t = classify(c, prev_is_num)
        # '项目|缩写|结果|参考区间|单位' 这类版面，缩写列在结果之前。
        # 纯字母格若出现在数值/范围之前，几乎一定是缩写而不是单位
        # （单位总在结果之后），否则会得到 '总蛋白 65.9 TP'。
        if t == 'unit' and not value and not val_cands and not rng \
                and CODE_RE.fullmatch(c):
            t = 'code'
        # 'A/G'、'T/C' 这类双大写字母比值是缩写而非单位
        # （真实单位 g/L、U/L、mmol/L 左右两侧不会都只有一个大写字母）
        # 限定"出现在数值之前"才这样判，否则会把结果后面的 'U/L' 也当成缩写
        if t == 'unit' and not value and not val_cands and not rng \
                and RATIO_ABBR.fullmatch(c):
            t = 'code'
        if t == 'range' and not rng:
            rng = c.strip('()（）【】[] ')
        elif t == 'value':
            val_cands.append((idx, c))
        elif t == 'name' and not name:
            name = c
            name_idx = idx
        elif t == 'name' and name and not value and not val_cands \
                and idx == name_idx + 1 \
                and len(c) <= 16 and not is_noise_label(c) \
                and not RATIO_FULL.match(c) and not CODE_RE.fullmatch(c):
            # 定性结果：便常规/尿常规/白带常规里大量 '结果' 是文字
            # （Negative、黄色、软便、完全液化）。只认紧挨项目名的那一格，
            # 免得把后面的'检验方法'列（双缩脲法）当成结果。
            #
            # 但要排除比值/代码形态：'HDL/CHOL' 后面紧跟的 'HDL/CHL' 是缩写，
            # 被当成定性结果就会得到 item_value='HDL/CHL'（真实值是 30.1）。
            value = c
        elif t == 'code' and not code:
            code = c
        elif t == 'unit' and not unit:
            unit = c
        elif t == 'value_glued':
            if re.match(r'^[<>≤≥＜＞]', c):
                # '<30mg/dl' 这种带单位尾巴的单侧范围，先存着，别当结果
                rng_cands.append(c)
            elif not value and not val_cands:
                # '7.1 ( 10^{{9}}/L ) 正常' → value=7.1, unit=10^{{9}}/L
                num, tail = split_num_prefix(c)
                if num and not CN_RE.search(num):
                    value = num
                    if not unit:
                        u = re.sub(r'[\u4e00-\u9fff]', '', tail).strip(' ()（）')
                        if u and len(u) <= 20:
                            unit = u
        elif t == 'other' and not value and not val_cands:
            # 值与单位粘连：'7.1 ( 10^9' 这类，拆出数值
            num, rest = split_num_prefix(c)
            if num and not CN_RE.search(num):
                value = num
                if not unit and rest and not CN_RE.search(rest):
                    unit = rest

    # 决定哪个数值格是结果。
    #
    # 踩过的坑：'No.|检验项目|英文代码|结果|…' 这类表格，OCR 有时把代码列
    if val_cands:
        if len(val_cands) >= 2 and val_cands[1][0] == val_cands[0][0] + 1 \
                and not ONE_SIDED_RANGE.match(val_cands[1][1]):
            value = val_cands[1][1]
            rest = val_cands[2:]
        else:
            if not value:
                value = val_cands[0][1]
            rest = val_cands[1:]
        for _, c in rest:
            # 剩下的数值里，带 <、>、≤、≥ 前缀的是单侧参考范围。
            if not rng and re.match(r'^[<>≤≥＜＞]', c or ''):
                rng = c
    if not rng:
        for c in rng_cands:
            rng = c
            break
    # 异常箭头单独成标记，不留在数值里（异常判定交给规则层）
    value = re.sub(r'[↑↓]', '', value or '').strip()
    unit = re.sub(r'[↑↓]', '', unit or '').strip()
    # 名称优先用中文名，代码只作兜底。
    # 依据：MedRepBench 金标准 94% 是中文名（4540 vs 287）；自采报告也期望中文名。
    label = name or code
    if not label:
        return None
    if NUM_FULL.fullmatch(label):
        return None
    if len(label) > 40:
        return None
    if WATERMARK_RE.match(label.replace(' ', '')):
        return None
    if is_noise_label(label):
        return None
    return {'item_name': label, 'item_code': code, 'item_value': value,
            'item_unit': unit, 'item_range': rng, 'is_abnormal': ''}


def strip_leading_serial(cells):
    """去掉行首序号列/水印列。真实报告常有 '| 1 | 项目 | 值 |' 结构，
    序号是 1-2 位小整数，不删会被当成结果值（实测 value recall 因此只有20%）。
    只删"后面紧跟项目名/代码"的情况，避免误删真正的数值结果（如血小板计数313）。
    另外 '黔HR 12' 这类水印也会占掉行首一格，同样需要跳过。"""
    changed = True
    while changed and len(cells) >= 2:
        changed = False
        head = (cells[0] or '').strip()
        nxt = (cells[1] or '').strip()
        if not head:
            cells = cells[1:]
            changed = True
        elif WATERMARK_RE.match(head):
            cells = cells[1:]
            changed = True
        elif re.fullmatch(r'\d{1,2}', head) and (CN_RE.search(nxt) or CODE_RE.fullmatch(nxt)):
            cells = cells[1:]
            changed = True
    return cells


def extract_row_items(row):
    """一行可能含左右两栏，按代码/名称切组后逐组提取。"""
    cells = [clean_cell(c) for c in row]
    if not any(cells):
        return []
    cells = strip_leading_serial(cells)
    groups, cur = [], []
    for idx, c in enumerate(cells):
        prev_is_num = bool(cur) and bool(NUM_FULL.fullmatch(cur[-1] or ''))
        t = classify(c, prev_is_num)
        split = False
        if t == 'code' and any(classify(x) in ('value', 'range') for x in cur):
            split = True
        elif t == 'name' and any(classify(x) in ('value', 'value_glued') for x in cur):
            # 双栏版面：一行里有两组"项目名|结果|单位|参考区间"，
            # 第二个项目名之后还跟着数值，说明这是右栏的起点。
            #
            split = any(NUM_FULL.fullmatch(x or '') or RANGE_FULL.match(x or '')
                        for x in cells[idx + 1: idx + 4])
        if split:
            groups.append(cur)
            cur = [c]
        else:
            cur.append(c)
    if cur:
        groups.append(cur)

    items = []
    for g in groups:
        it = build_item(g)
        if it:
            items.append(it)
    return items


def main():
    args = parse_args()
    files = sorted(glob.glob(os.path.join(args.artifacts, '*.json')))
    files = [f for f in files if not os.path.basename(f).startswith('_')]
    if not files:
        print('artifacts 目录里没有 json:', args.artifacts)
        sys.exit(1)
    print(f'artifacts: {len(files)} 份')

    if args.dump:
        for f in files[:args.dump]:
            rec = json.load(open(f, encoding='utf-8'))
            print('\n' + '=' * 70)
            print('FILE:', os.path.basename(f), '| gt项数:', rec.get('num_items_gt'))
            print('=' * 70)
            blocks = extract_blocks(rec)
            print(f'版面块数: {len(blocks)}')
            for bi, b in enumerate(blocks):
                c = b.get('content') or ''
                print(f'  [{bi}] label={b["label"]:<8} content长度={len(c)}'
                      f' {"<含table>" if "<table" in c.lower() else ""}')
            htmls = get_html_sources(rec)
            print(f'含表格HTML块: {len(htmls)}')
            tables = extract_tables(htmls)
            print(f'解析出表格数: {len(tables)}')
            for ti, tb in enumerate(tables[:2]):
                print(f'表格[{ti}] 行数={len(tb)} 首行={tb[0][:6] if tb else None}')
                for r in tb[1:4]:
                    print('   ', r[:6])
            items = []
            for tb in tables:
                for row in tb:
                    items.extend(extract_row_items(row))
            print(f'\n抽取结果: {len(items)} 项，前5项:')
            for it in items[:5]:
                print('   ', it)
        return

    out_lines, report = [], []
    stats = collections.Counter()
    gt_map = {}

    if args.gold_dir:
        for name in ('manifest.json', 'upload_manifest.json'):
            mp = os.path.join(args.gold_dir, name)
            if os.path.isfile(mp):
                for s in json.load(open(mp, encoding='utf-8'))['samples']:
                    gt_map[s['report_id']] = s
                break

    for f in files:
        rec = json.load(open(f, encoding='utf-8'))
        rid = rec.get('report_id') or os.path.splitext(os.path.basename(f))[0]
        gt_n = rec.get('num_items_gt') or gt_map.get(rid, {}).get('num_items')
        blocks = extract_blocks(rec)
        tables = extract_tables(get_html_sources(rec))

        all_items = []
        for tb in tables:
            for row in tb:
                all_items.extend(extract_row_items(row))

        seen, uniq = set(), []
        for it in all_items:
            key = (it['item_name'], it['item_value'])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)

        out_lines.append(json.dumps({
            'image': rec['image'],
            'report_date': extract_report_date(blocks),
            'items': uniq,
        }, ensure_ascii=False))
        stats['samples'] += 1
        stats['items_total'] += len(uniq)
        if not blocks:
            stats['no_block'] += 1
        if not tables:
            stats['no_table'] += 1
        if not uniq:
            stats['no_item'] += 1
        report.append({'report_id': rid, 'gt': gt_n, 'blocks': len(blocks),
                       'tables': len(tables), 'items': len(uniq)})

    with open(args.out, 'w', encoding='utf-8') as fo:
        fo.write('\n'.join(out_lines))

    print('\n' + '=' * 58)
    print('解析完成')
    print('=' * 58)
    print(f'样本 {stats["samples"]}  抽出项目 {stats["items_total"]}  '
          f'平均 {stats["items_total"]/max(1,stats["samples"]):.1f} 项/份')
    print(f'无版面块 {stats["no_block"]}  无表格 {stats["no_table"]}  '
          f'无项目 {stats["no_item"]}')
    print(f'输出: {args.out}')

    with open(args.report, 'w', encoding='utf-8') as fr:
        fr.write('# 解析报告\n\n| report_id | GT项数 | 版面块 | 表格数 | 抽出项数 |\n')
        fr.write('| --- | --- | --- | --- | --- |\n')
        for r in report:
            fr.write(f'| {r["report_id"]} | {r["gt"]} | {r["blocks"]} | '
                     f'{r["tables"]} | {r["items"]} |\n')
    print(f'明细: {args.report}')

    if stats['no_item'] == stats['samples']:
        print('\n!! 全部样本都没抽出项目，请先运行 --dump 查看实际输出结构')


if __name__ == '__main__':
    main()
