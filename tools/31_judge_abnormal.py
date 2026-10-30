# -*- coding: utf-8 -*-
r"""31 本地：异常判断规则引擎（把结果与参考区间比对，输出 normal/abnormal/unknown）

设计原则：
    1. **三态，不是二态**。信息不足时输出 unknown 而不是猜。
       医疗场景里"漏判"和"误判"代价不对称，宁可拒判让人复核。
    2. **只用报告自己给的参考区间**，不查外部知识库。
       报告印了 `4-10` 就按 4-10 判，不去查"这个医院的标准是不是 3.5-9.5"。
       这样判定结果可复现、可追责，也是 MedRepBench 金标准的定义方式。
    3. **区分置信度**。明确词（Negative/Positive）高置信；
       符号（+/++）低置信，因为 `++` 在不同项目上含义不同（尿胆原++ 可能正常）。

用法：
    # 给预测结果补上 is_abnormal
    python tools\31_judge_abnormal.py --predictions predictions.jsonl \
        --out predictions_flagged.jsonl"""

import os
import re
import sys
import json
import glob
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

NORMAL = 'normal'
ABNORMAL = 'abnormal'
UNKNOWN = 'unknown'

ARROW_RE = re.compile(r'[↑↓]')
# 区间：4-10 / 4--10 / 4~10 / 4～10 / 4—10（先匹配双横线，否则会被单横线吃掉负号）
RANGE_RE = re.compile(
    r'^([<>≤≥＜＞])?\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)'
    r'\s*(?:--|—|–|~|～|-)\s*'
    r'([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*$')
ONE_SIDED_RE = re.compile(r'^([<>≤≥＜＞])\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*$')
NUM_RE = re.compile(r'^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$')
# 数值后面粘了单位：'4.40x10^12'、'228x10^9'、'1.050E+08'
NUM_UNIT_RE = re.compile(
    r'^([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)'
    r'\s*(?:[x×]\s*10\s*\^?\s*(\d+)|(10\s*\^?\s*(\d+)))?\s*([A-Za-z%/μ]*)\s*$')

# ---------- 定性词表（明确定义，高置信）----------
# 只适用于**短值**。长文本是描述性结论（病理/影像/体格检查），
# 里面常常同时出现"未见"和"增生"这类相反信号，词表匹配必错，一律拒判。
SIGN_MAX_LEN = 16
# 顺序很重要：必须先认"否定短语"，否则 '未见异常' 会命中阳性词 '异常' 而被判成异常。
# 曾经把 '异常'、'检出' 放进 POS_WORDS，导致 '未见异常'、'未闻及异常'、
# '未检出' 全部误判为 abnormal（46 个误报的主因）。
NEG_PHRASES = ('未见', '未闻及', '未检出', '未找到', '未发现', '无异常',
               '无明显', '阴性', '正常', 'negative', 'neg')
POS_WORDS = ('positive', 'pos', '阳性', '弱阳性')
# 出现这些词说明句子不是单纯的定性结论，拒判
SIGN_EXCLUDE = ('非典型', '不除外', '可疑', '增生', '病变', '建议', '复查',
                '不正常', '未见异常细胞?见')


def sign_word(text):
    """定性词判定。返回 (normal/abnormal/None, 置信度)。

    只处理短值（≤ SIGN_MAX_LEN）。长文本一律返回 None 交给上层拒判——
    长描述性结论里常常同时出现相反信号（'系膜细胞和基质增生,未见电子致密物沉积'），
    词表匹配必错。
    """
    s = (text or '').strip()
    if not s or len(s) > SIGN_MAX_LEN:
        return None, ''
    low = s.lower()
    for w in SIGN_EXCLUDE:
        if w in low:
            return None, ''
    # 否定短语优先于阳性词
    for w in NEG_PHRASES:
        if w in low:
            return NORMAL, 'high'
    for w in POS_WORDS:
        if w in low:
            return ABNORMAL, 'high'
    # 符号型：'-' 表示阴性比较可靠；'+' 系列含义依赖项目，标低置信
    stripped = re.sub(r'[()（）\s]', '', low)
    if stripped in ('-', '—', '–'):
        return NORMAL, 'high'
    if re.fullmatch(r'[\u2295⊙\ufe0f]*\+{1,4}', stripped):
        return ABNORMAL, 'low'
    return None, ''


def parse_number(s):
    """容错数值解析。返回 (数值 or None, 是否带箭头)。

    处理 '↓4.5'、'4.5↑'、'4.40x10^12'、'1.050E+08'、'<0.05'
    """
    if s is None:
        return None, False
    t = str(s).strip()
    if not t:
        return None, False
    has_arrow = bool(ARROW_RE.search(t))
    t = ARROW_RE.sub('', t).replace(' ', '')
    t = t.lstrip('<>≤≥＜＞')
    if NUM_RE.match(t):
        try:
            return float(t), has_arrow
        except ValueError:
            return None, has_arrow
    m = NUM_UNIT_RE.match(t)
    if m:
        try:
            base = float(m.group(1))
        except ValueError:
            return None, has_arrow
        # '4.40x10^12' 的数值本体是 4.40，指数部分只是单位
        return base, has_arrow
    return None, has_arrow


def parse_range(raw):
    """把参考区间原文解析成结构化。

    返回 {'kind': interval|one_sided_low|one_sided_high|text|missing,
          'low': float|None, 'high': float|None,
          'op': '<' | '≤' | '>' | '≥' | None, 'raw': str}"""
    s = '' if raw is None else str(raw).strip()
    s = s.replace('＜', '<').replace('＞', '>')
    if not s:
        return {'kind': 'missing', 'low': None, 'high': None, 'op': None, 'raw': s}
    # 纯符号等于没填
    if re.fullmatch(r'[-－—–~～\s./|、()]+', s):
        return {'kind': 'missing', 'low': None, 'high': None, 'op': None, 'raw': s}

    packed = _pack_range(s)
    if packed:
        kind, a, b, op = packed
        if kind == 'interval':
            low, high = a, b
            if low > high:
                low, high = high, low
            return {'kind': 'interval', 'low': low, 'high': high,
                    'op': None, 'raw': s}
        if op in ('<', '≤'):
            return {'kind': 'one_sided_high', 'low': None, 'high': a,
                    'op': op, 'raw': s}
        return {'kind': 'one_sided_low', 'low': a, 'high': None,
                'op': op, 'raw': s}

    return {'kind': 'text', 'low': None, 'high': None, 'op': None, 'raw': s}


NUM_PAT = r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?'
# 区间后面允许跟的"纯单位尾巴"：ml、%、/ml、×10^6/ml，不含中文和裸数字
UNIT_TAIL_RE = re.compile(
    r'^(?:[×x]\s*10\s*\^?\s*\d+\s*)?[A-Za-z%/μµ.\s]*$')


def _pack_range(s):
    """识别 [操作符]数值[单位] 或 数值-数值[单位]。返回 (kind, a, b, op) 或 None。"""
    t = s.replace(' ', '')
    m = re.match(r'^([<>≤≥])?(' + NUM_PAT + r')(?:--|—|–|~|～|-)(' + NUM_PAT + r')(.*)$', t)
    if m and UNIT_TAIL_RE.match(m.group(4)):
        try:
            return ('interval', float(m.group(2)), float(m.group(3)), None)
        except ValueError:
            return None
    m = re.match(r'^([<>≤≥])(' + NUM_PAT + r')(.*)$', t)
    if m and UNIT_TAIL_RE.match(m.group(3)):
        try:
            return ('one_sided', float(m.group(2)), None, m.group(1))
        except ValueError:
            return None
    return None


def judge(value, range_raw):
    """核心判定。返回 {'flag', 'reason', 'confidence'}。

    参数用"字符串 + 字符串"，这样同一套规则既能吃金标准的结构化 reference，
    也能吃解析器抽出来的 item_range 文本，两条路径口径完全一致。
    """
    rr = parse_range(range_raw)
    v, arrow = parse_number(value)
    raw_value = '' if value is None else str(value).strip()
    has_value = raw_value != ''

    # 1) 数值区间：最可靠
    if rr['kind'] == 'interval':
        if v is None:
            # 值是定性词（'(-)'、'Negative'）而参考是数值区间：
            # 先试定性词，救回便常规/尿常规里的 '(-)' 一类
            w, conf = sign_word(raw_value)
            if w:
                return {'flag': w, 'reason': 'sign_word_over_interval',
                        'confidence': conf}
            return {'flag': UNKNOWN, 'reason': 'value_not_numeric', 'confidence': ''}
        if v < rr['low']:
            return {'flag': ABNORMAL, 'reason': 'below_low', 'confidence': 'high'}
        if v > rr['high']:
            return {'flag': ABNORMAL, 'reason': 'above_high', 'confidence': 'high'}
        return {'flag': NORMAL, 'reason': 'in_range', 'confidence': 'high'}

    # 2) 单侧区间
    # 边界一律按"闭区间"处理：实验室报告的 `<15` 意思是"15 及以下为正常"，
    # 不是"严格小于 15"。实测金标准里 `溶血指数 15 vs <15`、`PH值 4.5 vs <4.5`
    # 都判 normal，`FPSA/TPSA 0.15 vs ≥0.15` 也判 normal。
    if rr['kind'] == 'one_sided_high':
        if v is None:
            return {'flag': UNKNOWN, 'reason': 'value_not_numeric', 'confidence': ''}
        if v > rr['high']:
            return {'flag': ABNORMAL, 'reason': 'above_upper', 'confidence': 'high'}
        return {'flag': NORMAL, 'reason': 'under_upper', 'confidence': 'high'}

    if rr['kind'] == 'one_sided_low':
        if v is None:
            return {'flag': UNKNOWN, 'reason': 'value_not_numeric', 'confidence': ''}
        # '>N' 有两种语义：参考下限（≥N 正常）和阳性阈值（>N 即异常）。
        # 单看文本无法区分，标低置信，交由上层决定是否采信。
        if v >= rr['low']:
            return {'flag': NORMAL, 'reason': 'over_lower_ambiguous',
                    'confidence': 'low'}
        return {'flag': ABNORMAL, 'reason': 'below_lower', 'confidence': 'low'}

    # 3) 报告单自己标了 ↑/↓ —— 直接采信，这比我们算更权威
    if arrow:
        return {'flag': ABNORMAL,
                'reason': 'arrow_up' if '↑' in str(value) else 'arrow_down',
                'confidence': 'high'}

    # 4) 定性词
    w, conf = sign_word(raw_value)
    if w:
        return {'flag': w, 'reason': 'sign_word', 'confidence': conf}
    # 参考是定性词，值是数值 → 需要医学阈值（如 'Negative(-)' 配 0.72 IU/mL）
    # 报告没给阈值，规则层不猜
    if rr['kind'] == 'text':
        w2, conf2 = sign_word(rr['raw'])
        if w2 is not None:
            return {'flag': UNKNOWN, 'reason': 'qualitative_needs_threshold',
                    'confidence': ''}
        return {'flag': UNKNOWN, 'reason': 'text_reference', 'confidence': ''}

    if not has_value:
        return {'flag': UNKNOWN, 'reason': 'no_value', 'confidence': ''}
    return {'flag': UNKNOWN, 'reason': 'no_rule', 'confidence': ''}


# ---------- 派生指标 ----------
# 有些项目本身就是"参数"而不是"指标"：身高 177cm、体重 63.8kg 单独看既没有
# 参考区间，也没有医学上的正常范围，但它们组合出来的 BMI 是明确的判定依据。
# 这类只能跨项目算，单项目 judge() 做不到。
BMI_LOW = 18.5      # 中国成人低体重界值
BMI_OVER = 24.0     # 超重界值
BMI_OBESE = 28.0    # 肥胖界值


def _height_m(v, unit):
    """身高统一成米。"""
    u = (unit or '').lower().replace(' ', '')
    if 'mm' in u:
        return v / 1000.0
    if u == 'm' or '米' in u:
        return v if v < 3 else v / 100.0
    # 无单位或 cm：按数值大小兜底（1.77 是米，177 是厘米）
    return v / 100.0 if v > 3 else v


def _weight_kg(v, unit):
    """体重统一成千克。国内体检报告偶有用'斤'的，差一倍不能不管。"""
    u = (unit or '').lower()
    if '斤' in u:
        return v / 2.0
    if 'lb' in u or '磅' in u:
        return v * 0.45359237
    return v


def bmi_category(bmi):
    """按中国成人标准（WS/T 428-2013）分级。返回 (flag, 说明)。"""
    if bmi < BMI_LOW:
        return ABNORMAL, 'BMI %.1f 偏瘦（<18.5）' % bmi
    if bmi < BMI_OVER:
        return NORMAL, 'BMI %.1f 正常（18.5-23.9）' % bmi
    if bmi < BMI_OBESE:
        return ABNORMAL, 'BMI %.1f 超重（24.0-27.9）' % bmi
    return ABNORMAL, 'BMI %.1f 肥胖（≥28.0）' % bmi


def judge_report(items):
    """整份报告判定：先逐项判，再用派生指标补空缺。

    只补 `unknown`，**不推翻**已有判定——如果报告自己印了身高的参考区间，
    就以报告为准，派生值不参与。
    """
    items = items or []
    results = [judge(it.get('item_value'), it.get('item_range')) for it in items]

    h = w = None
    h_idx = w_idx = -1
    for i, it in enumerate(items):
        name = (it.get('item_name') or '').replace(' ', '')
        v, _ = parse_number(it.get('item_value'))
        if v is None:
            continue
        if '身高' in name and h is None:
            h, h_idx = _height_m(v, it.get('item_unit')), i
        # 排除'体重指数'和 BMI 本身，否则会把 BMI 当成体重再算一遍
        if ('体重' in name and '指数' not in name
                and 'BMI' not in name.upper() and w is None):
            w, w_idx = _weight_kg(v, it.get('item_unit')), i

    if h and w and 0.5 <= h <= 2.5 and 2 <= w <= 400:
        flag, note = bmi_category(w / (h * h))
        for i in (h_idx, w_idx):
            if results[i]['flag'] == UNKNOWN:
                results[i] = {'flag': flag, 'reason': 'derived_bmi',
                              'confidence': 'high', 'note': note}
    return results


SELFTEST = [
    # (value, range, 期望 flag, 说明)
    ('61.7', '40.0-75.0', NORMAL, '区间内'),
    ('19.40', '9.4--12.5', ABNORMAL, '双横线区间、超上限'),
    ('47', '80--160', ABNORMAL, '低于下限'),
    ('2.1', '0.8-1.2', ABNORMAL, '高于上限'),
    ('4.7', '4-10', NORMAL, '负号不被误当分隔符'),
    ('0.5', '<13.1', NORMAL, '单侧上限内'),
    ('15.31', '<10', ABNORMAL, '单侧上限外'),
    ('15', '<15', NORMAL, '等于上限算正常（实验室惯例）'),
    ('7', '≤7', NORMAL, '小于等于'),
    ('30', '≥35', ABNORMAL, '低于下限'),
    ('1026.09', '≥35', NORMAL, '高于下限'),
    ('0.15', '≥0.15', NORMAL, '等于下限算正常'),
    ('3.50', '≥2.0ml', NORMAL, '参考带单位'),
    ('34.91', '>60%', ABNORMAL, '参考带百分号'),
    ('26.89', '≥20×10^6/ml', NORMAL, '参考带数量级单位'),
    ('-26.78', '<4.0 为阴性，≥4.0 为阳性', UNKNOWN, '双阈值说明，不解析'),
    ('(-)', '0-1', NORMAL, '值是定性符号、参考是区间'),
    ('1.050E+08', '<2.000E+01', ABNORMAL, '科学计数法'),
    ('4.40x10^12', '3.50-5.20', NORMAL, '数值粘了单位'),
    ('Negative', 'Negative', NORMAL, '定性词一致'),
    ('Positive', 'Negative', ABNORMAL, '定性词相反'),
    ('阴性', '阴性', NORMAL, '中文定性词'),
    ('阳性', '阴性', ABNORMAL, '中文定性词相反'),
    ('3.059↑', '', ABNORMAL, '报告单自己标了上升箭头'),
    ('8.2', '', UNKNOWN, '无参考区间、无箭头 → 拒判'),
    ('Negative', 'Negative(-)', NORMAL, '带括号的定性词'),
    ('1.380', 'Negative(-)', UNKNOWN, '定性参考配数值 → 缺阈值，拒判'),
    ('', '', UNKNOWN, '无值无参考'),
    ('++', 'Negative', ABNORMAL, '符号型阳性（低置信）'),
    ('-', 'Negative', NORMAL, '符号型阴性'),
    ('非典型鳞状细胞', '未见', UNKNOWN, '否定前缀陷阱，不判阴性'),
]


def selftest():
    bad = 0
    for value, rng, want, note in SELFTEST:
        got = judge(value, rng)
        ok = got['flag'] == want
        if not ok:
            bad += 1
        print('%s %-14r %-14r → %-8s %-22s %s'
              % ('  OK ' if ok else ' FAIL', value, rng, got['flag'],
                 got['reason'], note))

    # 派生指标：身高体重单独看判不了，组合成 BMI 才能判
    print()
    print('派生指标（BMI）')
    cases = [
        ([('身高', '177', 'cm'), ('体重', '63.8', 'kg')], NORMAL,
         'BMI 20.4 正常'),
        ([('身高', '177', 'cm'), ('体重', '80', 'kg')], ABNORMAL,
         'BMI 25.5 超重'),
        ([('身高', '177', 'cm'), ('体重', '95', 'kg')], ABNORMAL,
         'BMI 30.3 肥胖'),
        ([('身高', '1.70', 'm'), ('体重', '45', 'kg')], ABNORMAL,
         '身高用米、BMI 15.6 偏瘦'),
        ([('身高', '177', 'cm'), ('体重', '127.6', '斤')], NORMAL,
         '体重用斤，换算后 BMI 20.4'),
        ([('身高', '177', 'cm'), ('体重指数(BMI)', '20.4', '')], UNKNOWN,
         '只有身高没有体重 → 不判'),
        ([('身高', '177', 'cm'), ('体重', '63.8', 'kg'),
          ('体重指数(BMI)', '20.4', '')], NORMAL, 'BMI 自身不被当成体重'),
    ]
    for rows, want, note in cases:
        items = [{'item_name': n, 'item_value': v, 'item_unit': u,
                  'item_range': ''} for n, v, u in rows]
        v = judge_report(items)
        flags = [x['flag'] for x in v]
        derived = [i for i, x in enumerate(v) if x['reason'] == 'derived_bmi']
        if want == UNKNOWN:
            ok = NORMAL not in flags and ABNORMAL not in flags
        else:
            ok = any(f == want for f in flags)
        if not ok:
            bad += 1
        print('%s %-46s → %-9s 派生项 %s  %s'
              % ('  OK ' if ok else ' FAIL', note, str(flags), derived, ''))

    print()
    print('通过 %d/%d' % (len(SELFTEST) + len(cases) - bad,
                          len(SELFTEST) + len(cases)))
    return 0 if bad == 0 else 1


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--predictions', default='')
    ap.add_argument('--out', default='')
    ap.add_argument('--gold', default='')
    ap.add_argument('--end2end', action='store_true',
                    help='与 --gold + --predictions 同时用：用抽取结果判异常，端到端评测')
    ap.add_argument('--report', default='abnormal_rule_report.md')
    ap.add_argument('--selftest', action='store_true')
    return ap.parse_args()


def load_gold(path):
    for f in glob.glob(os.path.join(path, '*.json')):
        if os.path.basename(f) in ('manifest.json', 'upload_manifest.json'):
            continue
        yield json.load(open(f, encoding='utf-8'))


def run_gold(gold_dir, report_path):
    """用金标准的 value + reference 评规则引擎本身，隔离抽取误差。"""
    cross = collections.Counter()
    conf_stat = collections.Counter()
    examples = collections.defaultdict(list)
    reasons = collections.Counter()
    # 按"判定原因"统计错误，定位是哪条规则在出错
    per_reason = collections.Counter()

    for d in load_gold(gold_dir):
        for it in d['items']:
            ref = it.get('reference') or {}
            rng = ref.get('raw')
            if rng is None:
                rng = ref_to_text(ref)
            got = judge(it.get('value'), rng)
            gt = it.get('flag') or UNKNOWN
            cross[(got['flag'], gt)] += 1
            reasons[got['reason']] += 1
            if got['flag'] in (NORMAL, ABNORMAL):
                conf_stat['judged:' + got['confidence']] += 1
                per_reason['all:' + got['reason']] += 1
                if got['flag'] == gt:
                    conf_stat['ok:' + got['confidence']] += 1
                elif gt in (NORMAL, ABNORMAL):
                    per_reason['err:' + got['reason']] += 1
                    k = 'WRONG:' + got['reason'] + '→' + gt
                    if len(examples[k]) < 4:
                        examples[k].append((d['report_id'], it.get('name'),
                                            it.get('value'), rng))
            elif gt in (NORMAL, ABNORMAL):
                k = 'MISS:' + got['reason']
                if len(examples[k]) < 4:
                    examples[k].append((d['report_id'], it.get('name'),
                                        it.get('value'), rng))

    total = sum(cross.values())
    judged = sum(v for (g, _), v in cross.items() if g in (NORMAL, ABNORMAL))
    ok = sum(v for (g, t), v in cross.items() if g in (NORMAL, ABNORMAL) and g == t)
    wrong = judged - ok

    print('=' * 70)
    print('规则引擎评测（用金标准 value + reference，隔离抽取误差）')
    print('=' * 70)
    print('  总项目            %d' % total)
    print('  做出判定          %d  (%.1f%%)' % (judged, judged / total * 100))
    print('  拒判              %d  (%.1f%%)' % (total - judged,
                                               (total - judged) / total * 100))
    print('  判定正确          %d' % ok)
    print('  判定错误          %d' % wrong)
    if judged:
        print('  已判准确率        %.2f%%  (%d/%d)'
              % (ok / judged * 100, ok, judged))
    print()
    print('  分置信度：')
    for c in ('high', 'low'):
        j = conf_stat['judged:' + c]
        o = conf_stat['ok:' + c]
        if j:
            print('    %-6s 判定 %-5d 正确 %-5d 准确率 %.2f%%'
                  % (c, j, o, o / j * 100))
    print()
    print('  混淆矩阵（我方 × 金标准）：')
    gts = [NORMAL, ABNORMAL, UNKNOWN]
    print('    %-10s %8s %8s %8s' % ('', *gts))
    for g in gts:
        print('    %-10s %8d %8d %8d' % (g, *[cross[(g, t)] for t in gts]))
    print()
    print('  判定原因分布（含该规则自身的准确率）：')
    for k, v in reasons.most_common():
        j = per_reason['all:' + k]
        if j:
            e = per_reason['err:' + k]
            print('    %-28s %5d   已判 %-4d 错 %-4d 准确率 %6.2f%%'
                  % (k, v, j, e, (j - e) / j * 100))
        else:
            print('    %-28s %5d   全部拒判' % (k, v))
    if examples:
        print()
        print('  典型问题：')
        for k, exs in list(examples.items())[:12]:
            print('    %s' % k)
            for e in exs:
                print('      %-20s %-12s %s' % (e[1], e[2], e[3]))

    with open(report_path, 'w', encoding='utf-8') as fp:
        fp.write('# 异常判定规则引擎评测\n\n')
        fp.write(f'- 总项目 {total}\n')
        fp.write(f'- 做出判定 {judged}（{judged/total*100:.1f}%）\n')
        fp.write(f'- 拒判 {total-judged}（{(total-judged)/total*100:.1f}%）\n')
        if judged:
            fp.write(f'- 已判准确率 **{ok/judged*100:.2f}%**（{ok}/{judged}）\n')
        fp.write('\n## 分置信度\n\n| 置信度 | 判定 | 正确 | 准确率 |\n| --- | --- | --- | --- |\n')
        for c in ('high', 'low'):
            j = conf_stat['judged:' + c]
            o = conf_stat['ok:' + c]
            if j:
                fp.write(f'| {c} | {j} | {o} | {o/j*100:.2f}% |\n')
        fp.write('\n## 混淆矩阵\n\n| 我方\\金标准 | normal | abnormal | unknown |\n')
        fp.write('| --- | --- | --- | --- |\n')
        for g in gts:
            fp.write('| %s | %d | %d | %d |\n' % (g, *[cross[(g, t)] for t in gts]))
        fp.write('\n## 判定原因分布\n\n| 原因 | 数量 |\n| --- | --- |\n')
        for k, v in reasons.most_common():
            fp.write(f'| {k} | {v} |\n')
    print()
    print('报告:', report_path)
    return 0


def ref_to_text(ref):
    """把结构化 reference 还原成文本，供 judge 吃。"""
    kind = (ref.get('kind') or '').strip()
    low = ref.get('low')
    high = ref.get('high')
    if kind == 'interval' and low is not None and high is not None:
        return '%s-%s' % (low, high)
    if kind == 'one_sided':
        if high is not None:
            return '<%s' % high
        if low is not None:
            return '>%s' % low
    if kind == 'text':
        return ref.get('text') or ''
    return ''


def run_predictions(pred_path, out_path):
    rows = [json.loads(l) for l in open(pred_path, encoding='utf-8') if l.strip()]
    stat = collections.Counter()
    for r in rows:
        items = r.get('items') or []
        verdicts = judge_report(items)
        for it, got in zip(items, verdicts):
            it['is_abnormal'] = '' if got['flag'] == UNKNOWN else got['flag']
            it['abnormal_reason'] = got['reason']
            it['abnormal_confidence'] = got['confidence']
            if got.get('note'):
                it['abnormal_note'] = got['note']
            else:
                it.pop('abnormal_note', None)
            stat[got['flag']] += 1
            stat['reason:' + got['reason']] += 1
    with open(out_path, 'w', encoding='utf-8') as fo:
        for r in rows:
            fo.write(json.dumps(r, ensure_ascii=False) + '\n')
    total = sum(stat[k] for k in (NORMAL, ABNORMAL, UNKNOWN))
    print('项目 %d' % total)
    for k in (ABNORMAL, NORMAL, UNKNOWN):
        print('  %-9s %5d  (%.1f%%)' % (k, stat[k], stat[k] / total * 100))
    print()
    print('  判定原因：')
    for k, v in stat.most_common():
        if k.startswith('reason:'):
            print('    %-34s %5d' % (k[7:], v))
    print()
    print('输出:', out_path)
    return 0


def load_eval07():
    """复用 07_evaluate.py 的金标准加载与名称配对逻辑，保证两个脚本口径一致。"""
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '07_evaluate.py')
    spec = importlib.util.spec_from_file_location('eval07_for31', p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_end2end(gold_dir, pred_path, report_path):
    """端到端：用**我们自己抽出来的** value / range 判异常，再与金标准 flag 比。

    与 run_gold 的差别只有一个：输入从"金标准的 value+reference"换成"抽取结果"。
    两者相减就能看出抽取误差对异常判定的传导量。
    """
    ev7 = load_eval07()

    def _stem(p):
        """统一成不带扩展名、不带 _masked 的报告号。

        gold 用 report_id（无后缀），预测用图片文件名（常带 _masked.png），
        两边不归一化就永远配对不上（实测交集 0 项）。
        """
        s = os.path.splitext(os.path.basename(p or ''))[0]
        return re.sub(r'(_masked|_annot|_已脱敏|_脱敏)$', '', s, flags=re.I)

    labels = ev7.load_labels_from_gold(gold_dir)
    mp = os.path.join(gold_dir, 'manifest.json')
    if os.path.isfile(mp):
        m = json.load(open(mp, encoding='utf-8'))
        mk = {s['report_id']: s['image'] for s in m['samples']}
        labels = {mk.get(k, k): v for k, v in labels.items()}
    preds = ev7.load_predictions(pred_path)
    labels = {_stem(k): v for k, v in labels.items()}
    preds = {_stem(k): v for k, v in preds.items()}

    cross = collections.Counter()
    reasons = collections.Counter()
    per_reason = collections.Counter()
    examples = collections.defaultdict(list)
    name_miss = 0

    for img in sorted(set(labels) & set(preds)):
        pairs = ev7.match_items(preds[img], labels[img], loose=True)
        matched = {id(r) for _, r in pairs}
        name_miss += len([r for r in labels[img] if id(r) not in matched])
        # 派生指标要整份报告才能算，所以先整体判定，再按对象取结果
        pred_items = preds[img]
        verdicts = judge_report(pred_items)
        vmap = {id(p): v for p, v in zip(pred_items, verdicts)}
        for pred, ref in pairs:
            got = vmap.get(id(pred)) or judge(pred.get('item_value'),
                                              pred.get('item_range'))
            gt = ref.get('flag') or UNKNOWN
            cross[(got['flag'], gt)] += 1
            reasons[got['reason']] += 1
            if got['flag'] in (NORMAL, ABNORMAL):
                per_reason['all:' + got['reason']] += 1
                if got['flag'] != gt:
                    per_reason['err:' + got['reason']] += 1
                    if gt in (NORMAL, ABNORMAL):
                        k = 'WRONG:' + got['reason'] + '→' + gt
                        if len(examples[k]) < 3:
                            examples[k].append((ref.get('name'),
                                                pred.get('item_value'),
                                                pred.get('item_range')))
            elif gt in (NORMAL, ABNORMAL):
                k = 'MISS:' + got['reason']
                if len(examples[k]) < 3:
                    examples[k].append((ref.get('name'), pred.get('item_value'),
                                        pred.get('item_range')))

    total = sum(cross.values())
    judged = sum(v for (g, _), v in cross.items() if g in (NORMAL, ABNORMAL))
    ok = sum(v for (g, t), v in cross.items() if g in (NORMAL, ABNORMAL) and g == t)

    print('=' * 70)
    print('端到端评测（用抽取出的 value + range 判异常）')
    print('=' * 70)
    print('  已配对项目        %d（另有 %d 项名称未命中，未参与）' % (total, name_miss))
    print('  做出判定          %d  (%.1f%%)' % (judged, judged / total * 100))
    print('  判定正确          %d' % ok)
    print('  判定错误          %d' % (judged - ok))
    if judged:
        print('  已判准确率        %.2f%%' % (ok / judged * 100))
    print()
    print('  混淆矩阵（我方 × 金标准）：')
    gts = [NORMAL, ABNORMAL, UNKNOWN]
    print('    %-10s %8s %8s %8s' % ('', *gts))
    for g in gts:
        print('    %-10s %8d %8d %8d' % (g, *[cross[(g, t)] for t in gts]))
    print()
    print('  判定原因分布：')
    for k, v in reasons.most_common():
        j = per_reason['all:' + k]
        if j:
            e = per_reason['err:' + k]
            print('    %-28s %5d   已判 %-4d 错 %-4d 准确率 %6.2f%%'
                  % (k, v, j, e, (j - e) / j * 100))
        else:
            print('    %-28s %5d   全部拒判' % (k, v))
    if examples:
        print()
        print('  典型问题：')
        for k, exs in list(examples.items())[:10]:
            print('    %s' % k)
            for e in exs:
                print('      %-20s %-12s %s' % e)
    with open(report_path, 'w', encoding='utf-8') as fp:
        fp.write('# 异常判定端到端评测\n\n')
        fp.write(f'- 已配对 {total} 项（名称未命中 {name_miss} 项未参与）\n')
        fp.write(f'- 做出判定 {judged}（{judged/total*100:.1f}%）\n')
        if judged:
            fp.write(f'- 已判准确率 **{ok/judged*100:.2f}%**（{ok}/{judged}）\n')
        fp.write('\n## 混淆矩阵\n\n| 我方\\金标准 | normal | abnormal | unknown |\n')
        fp.write('| --- | --- | --- | --- |\n')
        for g in gts:
            fp.write('| %s | %d | %d | %d |\n' % (g, *[cross[(g, t)] for t in gts]))
        fp.write('\n## 判定原因\n\n| 原因 | 数量 | 已判 | 错 |\n| --- | --- | --- | --- |\n')
        for k, v in reasons.most_common():
            fp.write(f'| {k} | {v} | {per_reason["all:" + k]} | '
                     f'{per_reason["err:" + k]} |\n')
    print()
    print('报告:', report_path)
    return 0


def main():
    args = parse_args()
    if args.selftest:
        return selftest()
    if args.gold and args.predictions and args.end2end:
        return run_end2end(args.gold, args.predictions, args.report)
    if args.gold:
        return run_gold(args.gold, args.report)
    if args.predictions:
        out = args.out or (os.path.splitext(args.predictions)[0] + '_flagged.jsonl')
        return run_predictions(args.predictions, out)
    print('需要 --selftest / --gold / --predictions 之一')
    return 1


if __name__ == '__main__':
    sys.exit(main())
