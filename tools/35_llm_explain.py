# -*- coding: utf-8 -*-
"""35 本地：用大模型生成报告解读

输入 31_judge_abnormal.py 的判定结果，输出分段解读（sections）。
抽取只抄不算，解读要把数值讲成人话，两者约束方向相反，所以分成两步。

配置（环境变量，不写入文件）：
    LLM_BASE_URL / LLM_MODEL / LLM_API_KEY"""

import os
import re
import sys
import json
import time
import argparse
import importlib.util

sys.stdout.reconfigure(encoding='utf-8')

DISCLAIMER = (
    '本解读由系统根据化验单数值与报告给出的参考范围自动生成，仅供健康科普参考，'
    '不构成诊断结论，不提供用药建议，不能替代医生面诊。'
    '如有异常或身体不适，请及时到正规医疗机构就诊。'
)

SYSTEM_PROMPT = """你是一名健康信息解读助手。我会给你一份检验结果的结构化数据，
包含每项指标的数值、参考范围、异常标记，以及系统已经判定好的严重度分级。

你的任务不是复述数值——报告单上已经印了。你要提供报告单上没有的信息：
这项指标在身体里做什么、异常常见于哪些情况、接下来该怎么做。

【分级是既定事实，不要重新判断】
系统已按危急值和偏离幅度给每项异常定级：critical / marked / moderate / mild。
你的措辞必须与分级一致：
- critical：必须明确"请立即就医"，必要时提示急诊
- marked：必须明确"建议尽快就诊（数日内）"
- moderate：建议"近期就诊查明原因"
- mild：可以说"可先复查，持续或加重再就诊"
严禁把 critical 或 marked 描述成"轻微""无需紧张""可以观察"。

【可以说】
- 解释指标是什么、在身体里起什么作用
- 说明这类异常在人群中常见于哪些情况（多项并列，保留不确定性）
- 给出复查时间、建议就诊的科室、需要警惕的症状
- 饮食、作息、运动等生活方式建议
- 与历史报告的对比（如果给了历史数据）

【不可以说】
- 对个体下确定性结论："你有感染""你患有××""说明你的肝有问题"
- "确诊""一定是""就是××病"
- 任何药品名称、剂量、疗程
- "无需担心""可以放心""没有大碍""健康状况良好""一切正常"这类总评
- 对 flag=unknown 的项目给出倾向性结论

【措辞的关键】
讨论人群中的常见情况时，主语是"这个指标"，不是"你"：
  可以说："白细胞升高常见于感染、炎症、应激或剧烈运动后"
  不可以说："你的白细胞升高，说明你感染了"
前者是医学常识，后者是诊断。同一件事，责任完全不同。

【输出】
sections 固定 3 到 4 段，按顺序：
- status：本次整体情况。先说分级结论（最重的一项放最前，带具体数值），
  再概括其余异常。超过 3 项异常时按系统归类，不要逐一罗列。
  存在 unknown 项时提醒"另有 N 项无法判断"。
- meaning：这些指标大致反映什么，用日常语言讲清楚作用，
  并说明这类异常常见于哪些情况。
- action：接下来做什么。按上面的分级要求给出紧迫性，
  包含需要警惕的症状（出现哪些情况要立刻就医）。
- status（可选第 4 段）：仅当存在 flag=unknown 的项目时，
  单独列出并说明"报告未提供参考范围，本次无法判断"。
总长度不超过 600 字。

【JSON 格式（必须严格遵守）】
只输出一个 JSON 对象，sections 必须是数组，每个元素有 kind、title、body：
{
  "sections": [
    {"kind": "status",  "title": "本次情况", "body": "..."},
    {"kind": "meaning", "title": "指标说明", "body": "..."},
    {"kind": "action",  "title": "建议",     "body": "..."}
  ]
}
不要用 {"status": "...", "meaning": "..."} 这种扁平写法。"""


# 段落数硬上限。正常是 3-4 段，8 是给"多一个 unknown 段 + 模型自作主张多写一段"
# 留的余量。超过就说明模型在逐项罗列，输出量会失控（32 项的表格能写到 26 段）。
MAX_SECTIONS = 8


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--predictions', default='')
    ap.add_argument('--out', default='explanations.jsonl')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--only', default='',
                    help='只跑报告号包含该字符串的样本，如 --only 090019'
                         '（比 --limit 精确，方便固定同一份做模型对比）')
    ap.add_argument('--dry-run', action='store_true', help='只打印 prompt，不联网')
    ap.add_argument('--mock', action='store_true', help='不调 API，用假回复验证后续流程')
    ap.add_argument('--check', action='store_true', help='跑自动校验并出报告')
    ap.add_argument('--check-file', default='',
                    help='只重新校验已有的解读 jsonl，不调模型（需同时给 --predictions）')
    ap.add_argument('--report', default='explain_check.md')
    ap.add_argument('--temperature', type=float, default=0.3,
                    help='解读允许一点措辞变化，但不要太高（默认0.3）')
    ap.add_argument('--max-tokens', type=int, default=2500,
                    help='单份输出上限。新护栏要解释指标作用与常见情况，'
                         '输出比复读数值长，1500 容易把 JSON 截断')
    ap.add_argument('--timeout', type=int, default=120,
                    help='单次请求超时秒数。异常项多的报告输出长，75 秒不够')
    ap.add_argument('--retries', type=int, default=2,
                    help='失败重试次数（默认2）。撞限流时给 4，退避会拉长到分钟级')
    ap.add_argument('--delay', type=float, default=0,
                    help='每份之间间隔秒数。批量跑给 1~2，能显著减少限流')
    ap.add_argument('--resume', action='store_true',
                    help='续跑：跳过 --out 里已成功的份，只补降级的。'
                         '批量跑撞限流后用它收尾，不重复花钱')
    ap.add_argument('--debug-raw', action='store_true',
                    help='把模型原始返回写到 _raw_debug.txt，'
                         '用于诊断解析失败/被截断')
    ap.add_argument('--selftest', action='store_true',
                    help='验证校验器本身（用故意违规的样例）')
    return ap.parse_args()


def load_llm_client():
    """复用 19_llm_extract.py 里的 OpenAI 兼容客户端，避免两套实现漂移。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, '19_llm_extract.py')
    spec = importlib.util.spec_from_file_location('llm19', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def compact_items(items):
    """只把解读需要的事实字段传出去。

    刻意不传 item_code、abnormal_note 之外的内部字段：
    传给模型的东西越少，它能"发挥"的材料就越少。
    """
    out = []
    for it in items:
        name = (it.get('item_name') or '').strip()
        if not name:
            continue
        out.append({
            'name': name,
            'value': (it.get('item_value') or '').strip(),
            'unit': (it.get('item_unit') or '').strip(),
            'range': (it.get('item_range') or '').strip(),
            'flag': (it.get('is_abnormal') or 'unknown') or 'unknown',
        })
    return out


def build_messages(report_id, items, severity=None):
    compact = compact_items(items)
    abnorm = [x for x in compact if x['flag'] == 'abnormal']
    unknown = [x for x in compact if x['flag'] == 'unknown']

    payload = {
        'report_id': report_id,
        'items': compact,
    }
    if severity:
        # 分级是规则引擎算的既定事实，模型只能照着表达，不能自己重判。
        # 只传异常项——正常项的分级对表达没有信息量，只会占 token。
        payload['severity'] = {
            'max_level': severity.get('max_level'),
            'abnormal_items': severity.get('items') or [],
        }
    user = (
        '下面是本次化验单的结构化结果（JSON）。\n'
        'flag 含义：normal=在参考范围内，abnormal=超出参考范围，'
        'unknown=报告未提供参考范围因而无法判断。\n'
        'severity 是系统已判定的严重度：max_level 为整份最重一级，'
        'abnormal_items 里每项带 level 和判定依据。\n\n'
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + '\n\n'
        + '统计：共 %d 项，其中超出参考范围 %d 项，无法判断 %d 项。'
        % (len(compact), len(abnorm), len(unknown))
        + ('\n整体严重度：%s。\n' % severity.get('max_level')
           if severity else '\n')
        + '请按规则输出 JSON。'
    )
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user},
    ]


def parse_json_reply(text):
    """从模型回复里抠出 JSON 对象。容忍代码块包裹和前后废话。"""
    t = (text or '').strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    # 退一步：抓第一个完整的 {...}
    start = t.find('{')
    if start >= 0:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == '{':
                depth += 1
            elif t[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except ValueError:
                        break
    return None


def flatten_to_sections(parsed):
    """把扁平的 {status:..., meaning:..., action:...} 转成 sections。

    模型常忽略嵌套要求。约束格式是 prompt 的责任，解析器只负责尽量读懂。
    """
    if not isinstance(parsed, dict):
        return []
    buckets = []
    for k, v in parsed.items():
        if not isinstance(v, str) or not v.strip():
            continue
        key = str(k).lower().strip()
        body = v.strip()
        if 'unknown' in key or '无法判断' in key or '未提供' in key:
            buckets.append((4, 'status', '无法判断的项目', body))
        elif 'status' in key or '情况' in key or '状态' in key or '概述' in key:
            buckets.append((1, 'status', '本次情况', body))
        elif 'meaning' in key or '含义' in key or '意义' in key or '说明' in key:
            buckets.append((2, 'meaning', '指标说明', body))
        elif 'action' in key or '建议' in key or '行动' in key or '措施' in key:
            buckets.append((3, 'action', '建议', body))
    if not buckets:
        return []
    buckets.sort(key=lambda x: x[0])
    return [{'section_id': 's%d' % (i + 1), 'kind': kind, 'title': title,
             'body': body, 'item_id': None}
            for i, (_o, kind, title, body) in enumerate(buckets)]


def normalize_llm_sections(raw):
    """把模型输出规整成 ExplanationDraft 的 sections。

    模型可能给出 kind 之外的值、缺字段、或者干脆返回字符串。
    这一层负责"无论模型怎么乱，出来的结构都是合法的"。
    """
    KINDS = ('meaning', 'status', 'action')
    out = []
    for i, s in enumerate(raw or []):
        if len(out) >= MAX_SECTIONS:
            break                     # 保护：模型失控时不把界面撑爆
        if not isinstance(s, dict):
            continue
        kind = str(s.get('kind') or '').strip().lower()
        if kind not in KINDS:
            # 猜一个：含"建议/复查/运动"当 action，含"反映/作用"当 meaning
            blob = (str(s.get('title') or '') + str(s.get('body') or ''))
            if re.search(r'建议|复查|运动|饮食|作息|随访', blob):
                kind = 'action'
            elif re.search(r'反映|作用|意义|什么是|主要', blob):
                kind = 'meaning'
            else:
                kind = 'status'
        title = str(s.get('title') or '').strip()[:40]
        body = str(s.get('body') or '').strip()
        if not body:
            continue
        out.append({
            'section_id': 's%d' % (i + 1),
            'kind': kind,
            'title': title or ('指标说明' if kind == 'meaning' else
                               '当前情况' if kind == 'status' else '建议'),
            'body': body,
            'item_id': None,
        })
    return out


def clean_ref(raw):
    """参考范围展示清洗：OCR 的 4.4--11.9 显示成 4.4-11.9。"""
    s = '' if raw is None else str(raw).strip()
    for a, b in (('＜', '<'), ('＞', '>'),
                 ('～', '-'), ('~', '-'), ('—', '-'), ('–', '-')):
        s = s.replace(a, b)
    s = re.sub(r'-{2,}', '-', s)
    s = re.sub(r'\s*-\s*', '-', s)
    s = re.sub(r'\s*([<>≤≥])\s*', r'\1', s)
    return s.strip()


def load_grader():
    """分级器按需加载，避免与 45 的依赖形成环。"""
    global _GRADER
    if _GRADER is None:
        _GRADER = load_mod('45_grade_severity.py', 'm45')
    return _GRADER


_GRADER = None


def is_unknown(x):
    """三态里的 unknown。

    不能写成 `not x.get('is_abnormal')`：'unknown' 是非空字符串，为真，
    那样写会把所有"无法判断"的项目漏掉，模板里一个字都不提。
    """
    return (x.get('is_abnormal') or 'unknown') == 'unknown'


def fallback_sections(items):
    """模型不可用时的降级输出：纯模板，只用事实，不解释。

    降级比"编一段像模像样的解读"安全得多——读者能看出这是模板，
    但看不出一段错误解释是错的。
    """
    abnorm = [x for x in items if x.get('is_abnormal') == 'abnormal']
    unknown = [x for x in items if is_unknown(x)]
    secs = []
    if abnorm:
        lines = []
        for x in abnorm:
            rng = clean_ref(x.get('item_range')) or '（未提供）'
            lines.append('%s %s %s，参考范围 %s'
                         % (x.get('item_name'), x.get('item_value') or '',
                            x.get('item_unit') or '', rng))
        secs.append({
            'section_id': 's1', 'kind': 'status', 'item_id': None,
            'title': '超出参考范围的项目',
            'body': '本次共有 %d 项超出参考范围：\n%s'
                    % (len(abnorm), '\n'.join(lines)),
        })
    elif unknown:
        # 有项目拿不到参考范围时不能说"全部正常"：
        # "都落在范围内"与"有一项根本判不了"是不同结论，不能混
        secs.append({
            'section_id': 's1', 'kind': 'status', 'item_id': None,
            'title': '结果概况',
            'body': '本次已给出参考范围的 %d 项，数值均落在范围内；'
                    '另有 %d 项因报告未提供参考范围，无法判断是否异常。'
                    % (len(items) - len(unknown), len(unknown)),
        })
    else:
        secs.append({
            'section_id': 's1', 'kind': 'status', 'item_id': None,
            'title': '结果概况',
            'body': '本次所有项目的数值都落在报告给出的参考范围内。',
        })
    if unknown:
        secs.append({
            'section_id': 's2', 'kind': 'status', 'item_id': None,
            'title': '无法判断的项目',
            'body': '以下 %d 项报告未提供参考范围，无法判断是否异常：%s'
                    % (len(unknown),
                       '、'.join(x.get('item_name') or '' for x in unknown)),
        })
    secs.append({
        'section_id': 's%d' % (len(secs) + 1), 'kind': 'action',
        'item_id': None, 'title': '建议',
        'body': '请携带原始化验单咨询临床医生。单次检查的轻度异常常见，'
                '是否需要复查或进一步检查应由医生结合症状和病史判断。',
    })
    return secs


# ---------- 自动校验 ----------
# 越界词：出现即判定输出不合规
DIAG_WORDS = ('患有', '确诊', '诊断为', '你得了', '说明你有', '提示你有',
              '可诊断', '即为', '属于病')

# 总体健康评价。报告只覆盖部分指标，不能给整体结论。
# 只列具体短语，不含"正常"，避免误伤"在正常范围内"。
OVERALL_WORDS = ('健康状况良好', '健康状态良好', '身体状况良好',
                 '身体很健康', '非常健康', '一切正常', '完全正常',
                 '均属正常', '没有大碍', '无需担心', '可以放心')

# 软性医学推断。DIAG_WORDS 拦的是"你患有××"这种直说，
# 但"可能提示身体存在炎症、感染、血液系统疾病"同样越界——
# 它把指标等同疾病，而且更隐蔽：读者会觉得"只是可能"，照样吓人。
# 注意不能拦"提示"本身，"提示您注意休息"是正常说法。
# "可能提示""需警惕"属医学常识的正常表达，只要主语是指标而非患者即可；
# 真正要拦的是确定性断言与个体化结论。
HINT_WORDS = ('可能患有', '考虑为', '不排除患', '提示你患')

# 确定性断言：把一个可能性说成结论
CERTAIN_WORDS = ('确诊', '一定是', '肯定是', '就是', '必然是',
                 '说明你有', '证明你有', '确定为')

# 个体断言：主语从"指标"滑到"你"。
# 用正则是因为固定短语抓不全（"说明你感染了"）。
PERSONAL_WORDS = ('你患有', '你得了', '你的病', '你有病')
PERSONAL_RE = re.compile(
    r'(说明|提示|表明|意味着|证实)你(有|患|得|感染|存在|是)')
DRUG_WORDS = ('服用', '口服', '剂量', 'mg/', '一天两次', '一日三次',
              '疗程', '片/日', '注射液', '胶囊')


def numbers_in(text):
    """提取文本里所有数值，用于"有没有编造数据"的检查。"""
    xs = []
    for m in re.finditer(r'\d+(?:\.\d+)?', text or ''):
        try:
            xs.append(float(m.group(0)))
        except ValueError:
            pass
    return xs


def allowed_numbers(items):
    """允许在解读里出现的数值：报告结果值、参考区间边界、叙述数字。

    叙述数字指项目计数、建议时长、百分比，不放行会误报。
    """
    ok = set()
    for x in items:
        # 认两种字段名：predictions 用 item_value/item_range，
        # 复核工具用 value/range
        for k in ('value', 'range', 'item_value', 'item_range'):
            for v in numbers_in(x.get(k) or ''):
                ok.add(v)
    # 计数：总项数、异常项数、无法判断项数
    ok.add(float(len(items)))
    ok.add(float(sum(1 for x in items if x.get('is_abnormal') == 'abnormal')))
    ok.add(float(sum(1 for x in items if is_unknown(x))))
    return ok


# 建议里的生活计量单位，数字后跟这些不按检测值处理
LIFE_UNITS = ('分钟', '小时', '天', '次', '周', '岁', '个月', '年',
              '公斤', '千克', '克', '米', '公里', '步')


def name_variants(nm):
    """项名在正文里的可能写法。

    抽取层把名称与编码合成一个字段（"淋巴细胞绝对值(LYM#)"），
    而模型写正文时通常不带编码，只按原样匹配会漏掉长名。
    """
    out = [nm]
    base = re.sub(r'[（(][^）)]*[）)]\s*$', '', nm).strip()
    if base and base != nm:
        out.append(base)
    return out


def independent_names(text, names):
    """返回在 text 里真正独立出现的项名。

    "淋巴细胞"会在"淋巴细胞绝对值"里命中，但那是另一项。
    判据是出现位置：有一次不被更长名字覆盖，即算独立提到。
    """
    pairs = []                      # (匹配用变体, 所属原名)
    for nm in names:
        for v in name_variants(nm):
            pairs.append((v, nm))

    spans = {}
    for v, _nm in pairs:
        pos, i = [], text.find(v)
        while i >= 0:
            pos.append((i, i + len(v)))
            i = text.find(v, i + 1)
        if pos:
            spans.setdefault(v, []).extend(pos)

    out = []
    for nm in names:
        mine = [v for v, o in pairs if o == nm]
        others = [(v, o) for v, o in pairs if o != nm]
        independent = False
        for v in mine:
            for (a, b) in spans.get(v, []):
                covered = False
                for v2, _o2 in others:
                    if any(a >= a2 and b <= b2
                           for (a2, b2) in spans.get(v2, [])):
                        covered = True
                        break
                if not covered:
                    independent = True
                    break
            if independent:
                break
        if independent:
            out.append(nm)
    return out


def suspicious_numbers(text, allowed):
    """挑出疑似编造的检测值数字。

    只查两类：带小数的、大于 100 的整数。整数且 ≤100 放行，那是计数与时长的量级。
    """
    out = []
    src = text or ''
    for m in re.finditer(r'\d+(?:\.\d+)?', src):
        tok = m.group(0)
        try:
            v = float(tok)
        except ValueError:
            continue
        if v in allowed:
            continue
        if '.' not in tok and v <= 100:
            continue
        # 建议里的生活计量不是检测值：'每周150分钟有氧活动' 的 150
        # 不是编造出来的化验结果，不该被当成幻觉数值
        tail = src[m.end():m.end() + 4].lstrip()
        if any(tail.startswith(u) for u in LIFE_UNITS):
            continue
        out.append(v)
    return out


# critical/marked 级出现这些词就是"淡化"，比任何疾病词都危险
SOOTHE_WORDS = ('轻微', '无需紧张', '不用管', '不必担心', '没什么问题',
                '无需治疗', '可以先观察', '问题不大')
SEEK_WORDS = ('就诊', '就医', '急诊', '复查', '门诊', '挂号')


def check_explanation(rec, items, severity=None):
    """对一份解读做自动校验，返回问题列表。

    severity 给了才做分级一致性检查——没有分级信息时无从判断措辞是否匹配。
    """
    issues = []
    secs = rec.get('sections') or []
    text = ' '.join((s.get('title') or '') + ' ' + (s.get('body') or '')
                    for s in secs)

    if not secs:
        issues.append(('结构', 'sections 为空'))
    kinds = {s.get('kind') for s in secs}
    if 'status' not in kinds:
        issues.append(('结构', '缺少 status 段（没有说明当前情况）'))
    if not rec.get('disclaimer'):
        issues.append(('合规', '缺少免责声明'))

    for w in DIAG_WORDS:
        if w in text:
            issues.append(('越界', '出现诊断性表述：%r' % w))
    for w in CERTAIN_WORDS:
        if w in text:
            issues.append(('越界', '把可能性说成结论：%r' % w))
    for w in PERSONAL_WORDS:
        if w in text:
            issues.append(('越界', '对个体下断言：%r' % w))
    m = PERSONAL_RE.search(text)
    if m:
        issues.append(('越界', '对个体下断言：%r' % m.group(0)))

    # 分级与措辞必须一致：危急项配上"轻微/无需担心"是比疾病词更严重的错误
    if severity:
        lvl = severity.get('max_level')
        if lvl in ('critical', 'marked'):
            hit = [w for w in SOOTHE_WORDS if w in text]
            if hit:
                issues.append(
                    ('逻辑', '分级为 %s，却用了淡化措辞：%r' % (lvl, hit[0])))
            if not any(w in text for w in SEEK_WORDS):
                issues.append(
                    ('逻辑', '分级为 %s，但通篇没有任何就医或复查建议' % lvl))
    for w in DRUG_WORDS:
        if w in text:
            issues.append(('越界', '出现用药建议：%r' % w))
    hit_hint = [w for w in HINT_WORDS if w in text]
    if hit_hint:
        issues.append(('越界', '把指标推断成疾病：%r' % hit_hint[0]))
    hit = [w for w in OVERALL_WORDS if w in text]
    if hit:
        issues.append(('越界', '给出总体健康评价：%r（报告只覆盖部分指标）'
                       % hit[0]))

    # 幻觉数值：解读里出现了报告没有的数字
    ok = allowed_numbers(items)
    bad = sorted(set(suspicious_numbers(text, ok)))
    if bad:
        issues.append(('事实', '出现报告里没有的数值：%s'
                       % ', '.join('%g' % v for v in bad[:8])))

    # unknown 项不能被下结论。三个限定缺一不可，否则大面积误报：
    #   1. 按句子判断，不按整段——项名和结论词常常不在同一句
    #   2. 把项名从句子剔除——"异常白细胞"名字里就含"异常"，
    #      不剔除的话它会自己触发自己（实测形态学表 5 项全部误报）
    #   3. 先摘掉"无法判断/是否异常/未提供参考范围"这类合法说法
    safe = ('无法判断', '不能判断', '是否异常', '未提供参考范围',
            '没有参考范围', '参考范围缺失', '未给出参考范围')
    # 一次性收集所有无参考范围的项名，每句都要整批摘除。
    # 只摘当前项是不够的：句子"以下 5 项无法判断：异常白细胞、淋巴细胞…"
    # 在检查"淋巴细胞"时，"异常白细胞"仍留在句子里，它含的"异常"照样自触发。
    unknown_names = []
    for x in items:
        if is_unknown(x):
            nm = (x.get('item_name') or '').strip()
            if len(nm) >= 2:
                unknown_names.append(nm)
    # 判"是否提到了这一项"必须按独立出现判断，不能按子串包含：
    # 形态学表里"淋巴细胞"是无参考范围的项，而"淋巴细胞绝对值"是有范围的另一项，
    # 正文写出后者时前者也会命中，于是误报"对无法判断的项下了结论"
    all_names = [(x.get('item_name') or '').strip() for x in items]
    all_names = [x for x in all_names if len(x) >= 2]

    # 分级层已把某些"无参考范围"的项按绝对值/文本判定提了级别
    # （如原始幼稚细胞 77% → critical）。这些项已经不属于"无法判断"，
    # 模型提到它们是必须的，不能再按 unknown 拦——否则两级规则互相打架。
    graded = set()
    if severity:
        for x in severity.get('items') or []:
            nm = (x.get('name') or '').strip()
            if nm:
                graded.add(nm)

    flagged = set()
    for s in secs:
        for sent in re.split(r'[。；;！!？?\n]', s.get('body') or ''):
            if len(sent) < 4:
                continue
            indep = set(independent_names(sent, all_names))
            for nm in unknown_names:
                if nm in flagged or nm not in indep:
                    continue
                if nm in graded:
                    continue      # 已被分级，参与讨论是合法的
                tmp = sent
                for p in safe:
                    tmp = tmp.replace(p, '')
                for nm2 in unknown_names:
                    tmp = tmp.replace(nm2, '')
                if '正常' in tmp or '异常' in tmp:
                    flagged.add(nm)
                    issues.append(
                        ('事实', '%r 的参考范围缺失，却被下了结论' % nm))
    return issues


def run_check(out_rows, src_rows, report_path, verbose=True):
    """对已生成的解读跑自动校验并写报告。

    抽成函数是为了让 --check-file 能重新校验历史输出：
    改一条校验规则不该要求重新调一遍 API，那是真金白银，
    而且模型输出带随机性，重跑后对比的已不是同一批文本。
    """
    all_issues = {}
    for r in out_rows:
        src = next((x for x in src_rows
                    if (x.get('image') or x.get('report_id')) == r.get('report_id')),
                   None)
        if src is None:
            # 拿不到原始指标时绝不能当成"items 为空"继续跑：
            # 那样 allowed 数字集合几乎是空的，正文里每个小数都会被判成编造，
            # 结论看起来像"模型在胡说"，实际是校验器自己丢了输入
            all_issues[r.get('report_id')] = [
                ('内部', '找不到该报告的原始指标，无法校验数值，'
                         '本份结论不可信（检查 --predictions 是否完整）')]
            continue
        items = src.get('items') or []
        try:
            sev = load_grader().grade_report(items)
        except Exception:
            sev = None
        all_issues[r.get('report_id')] = check_explanation(r, items, sev)

    # 降级份必须单独标出来。模板是 100% 合规的，
    # 所以"校验通过"既可能是模型写得好，也可能模型根本没参与——
    # 这两种情况混在一起，报告就失去了判断价值（实测两份不同来源的
    # 报告能做到字节级完全相同，因为它俩都是模板兜底）。
    by_id = {r.get('report_id'): r for r in out_rows}
    n_bad = sum(1 for v in all_issues.values() if v)
    n_deg = sum(1 for r in out_rows if r.get('degraded'))
    n_ok = len(out_rows) - n_bad - n_deg

    def verdict(rid, iss):
        r = by_id.get(rid) or {}
        if iss:
            return '有问题'
        if r.get('degraded'):
            return '通过（模板降级：%s）' % (r.get('degrade_reason') or '未记录原因')
        return '通过（模型生成）'

    if verbose:
        print()
        print('=' * 70)
        print('自动校验')
        print('=' * 70)
        for rid, iss in all_issues.items():
            r = by_id.get(rid) or {}
            if iss:
                print('  %s' % rid)
                for kind, msg in iss:
                    print('    [%s] %s' % (kind, msg))
            elif r.get('degraded'):
                print('  %s' % rid)
                print('    [降级] 模型未参与，已用模板兜底：%s'
                      % (r.get('degrade_reason') or '未记录原因'))
        print()
        print('  %d/%d 份有问题，%d 份降级，%d 份模型生成且合规'
              % (n_bad, len(out_rows), n_deg, n_ok))

    with open(report_path, 'w', encoding='utf-8') as fp:
        fp.write('# 解读自动校验\n\n')
        fp.write('- 总份数 %d，有问题 %d 份，降级 %d 份，模型生成且合规 %d 份\n'
                 % (len(out_rows), n_bad, n_deg, n_ok))
        if n_deg:
            fp.write('\n> 降级的份显示"通过"，是因为模板只陈述事实、不会违规。\n'
                     '> 它不代表模型正常工作，判断模型效果请看"模型生成"的份数。\n')
        fp.write('\n## 逐份结果\n\n')
        for rid, iss in all_issues.items():
            fp.write('### %s\n\n' % rid)
            if iss:
                for kind, msg in iss:
                    fp.write('- [%s] %s\n' % (kind, msg))
            else:
                fp.write('%s\n' % verdict(rid, []))
            fp.write('\n')
    if verbose:
        print('  报告: %s' % report_path)
    return n_bad


def selftest():
    """验证校验器本身有效——用故意违规的样例，看它能不能抓出来。

    一个只会在正常输入上通过的校验器等于没有校验器。
    """
    base_items = [
        {'item_name': '甘油三酯', 'item_value': '1.89', 'item_unit': 'mmol/L',
         'item_range': '0.00~1.70', 'is_abnormal': 'abnormal'},
        {'item_name': '总蛋白', 'item_value': '73.4', 'item_unit': 'g/L',
         'item_range': '65--85', 'is_abnormal': 'normal'},
        {'item_name': 'DeRitis比值', 'item_value': '0.77', 'item_unit': '',
         'item_range': '', 'is_abnormal': 'unknown'},
    ]

    def rec(sections, disclaimer=DISCLAIMER):
        return {'sections': sections, 'disclaimer': disclaimer}

    def sec(kind, title, body):
        return {'kind': kind, 'title': title, 'body': body}

    good = rec([
        sec('status', '一项超出参考范围', '甘油三酯 1.89 mmol/L，高于参考上限 1.70。'),
        sec('meaning', '指标说明', '甘油三酯是血脂的一种。总蛋白 73.4 g/L 在参考范围内。'),
        sec('action', '建议', '减少油腻饮食，3 个月后复查。'),
    ])

    # unknown 项提到的正确说法：只能说"无法判断"，不能被误报
    # 两个项名里都带"异常/细胞"字样，是误报的主要来源
    multi_unknown = [
        {'item_name': '异常白细胞', 'item_value': '0.30', 'item_unit': '',
         'item_range': '', 'is_abnormal': 'unknown'},
        {'item_name': '淋巴细胞', 'item_value': '0.45', 'item_unit': '',
         'item_range': '', 'is_abnormal': 'unknown'},
    ]

    # "淋巴细胞"（无范围）与"淋巴细胞绝对值"（有范围）是两个项目，
    # 正文只写后者时不能判成对前者下了结论
    substring_items = [
        {'item_name': '淋巴细胞绝对值(LYM#)', 'item_value': '13.89',
         'item_unit': '10^9/L', 'item_range': '1.8--6.3',
         'is_abnormal': 'abnormal'},
        {'item_name': '淋巴细胞', 'item_value': '0.45', 'item_unit': '',
         'item_range': '', 'is_abnormal': 'unknown'},
    ]

    ABS_ITEMS = [
        {'item_name': '原始幼稚细胞', 'item_value': '77.0', 'item_unit': '%',
         'item_range': '', 'is_abnormal': 'unknown'},
    ]

    good_unknown = rec([
        sec('status', '结果', '甘油三酯 1.89 mmol/L，高于参考上限 1.70。'),
        sec('status', '无法判断的项目',
            '以下 1 项因报告未提供参考范围，无法判断是否异常：'
            'DeRitis比值 0.77。'),
        sec('action', '建议', '减少油腻饮食，3 个月后复查。'),
    ])

    cases = [
        ('正常解读应无问题', good, 0),
        ('如实交代无法判断不误报', good_unknown, 0),
        ('诊断性表述', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L。'),
            sec('meaning', '说明', '你患有高脂血症。'),
        ]), 1),
        ('用药建议', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L。'),
            sec('action', '建议', '建议服用他汀类药物，疗程三个月。'),
        ]), 1),
        ('编造数值', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L，血糖 5.60 mmol/L。'),
        ]), 1),
        ('缺免责声明', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L。'),
        ], disclaimer=''), 1),
        ('缺 status 段', rec([
            sec('meaning', '说明', '甘油三酯是血脂的一种。'),
        ]), 1),
        ('对缺参考范围项下结论', rec([
            sec('status', '结果', 'DeRitis比值 0.77 属正常范围。'),
        ]), 1),
        ('总体健康评价', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L，总体健康状况良好。'),
        ]), 1),
        ('安慰性结论', rec([
            sec('status', '结果', '其余指标一切正常，无需担心。'),
        ]), 1),
        # 主语是指标、内容是人群知识 —— 新护栏下这是允许的，
        # 而且是模型价值所在。拦它等于回到"复读数值"。
        ('人群层推断应允许', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L。'),
            sec('meaning', '说明',
                '甘油三酯升高常见于高脂饮食、饮酒、肥胖或缺乏运动。'),
        ]), 0),
        # 同一件事，主语换成"你"，就从科普变成诊断
        ('个体化断言要抓到', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L。'),
            sec('meaning', '说明', '这说明你患有高脂血症。'),
        ]), 1),
        ('说明你感染了要抓到', rec([
            sec('status', '结果', '白细胞 12.0 10^9/L。'),
            sec('meaning', '说明', '白细胞升高说明你感染了。'),
        ]), 1),
        # 形态学分类表：多个项名自带"异常""粒细胞"字样，
        # 一句里全部列出时必须不误报
        ('多未知项如实列出不误报', rec([
            sec('status', '无法判断的项目',
                '以下 2 项因报告未提供参考范围，无法判断是否异常：'
                '异常白细胞 0.30、淋巴细胞 0.45。'),
        ]), 0, multi_unknown),
        ('多未知项真下结论要抓到', rec([
            sec('status', '结果',
                '异常白细胞 0.30 属正常范围，淋巴细胞 0.45 偏低。'),
        ]), 1, multi_unknown),
        # 子串冲突：只说了"淋巴细胞绝对值"，没说"淋巴细胞"
        ('短名被长名包含不误报', rec([
            sec('status', '结果',
                '27项中1项异常：淋巴细胞绝对值 13.89 10^9/L，略高。'),
        ]), 0, substring_items),
        # 分级提升过的项：校验器不能再按 unknown 拦住。
        # 这里必须带就医建议，否则会正确触发"缺失行动"检查（分级是 critical）
        ('已分级项可以讨论', rec([
            sec('status', '结果',
                '镜检见原始幼稚细胞 77.0%，显著异常，本次结果危急，请立即就医。'),
        ]), 0, ABS_ITEMS, {'max_level': 'critical',
                           'items': [{'name': '原始幼稚细胞',
                                      'level': 'critical',
                                      'reason': '高于预警阈值 5'}]}),
        # 运动建议里的"150分钟"不是编造的检测值
        ('建议里的生活计量不误报', rec([
            sec('status', '结果', '甘油三酯 1.89 mmol/L，高于参考上限 1.70。'),
            sec('action', '建议',
                '每周150分钟中等强度有氧活动，3个月后复查。'),
        ]), 0),
    ]

    bad = 0
    for case in cases:
        name, r, expect = case[0], case[1], case[2]
        its = case[3] if len(case) > 3 else base_items
        sev = case[4] if len(case) > 4 else None
        iss = check_explanation(r, its, sev)
        ok = (len(iss) > 0) == (expect > 0)
        if not ok:
            bad += 1
        print('%s %-22s 检出 %d 条%s'
              % ('  OK ' if ok else ' FAIL', name, len(iss),
                 '' if not iss else '：' + iss[0][0] + '/' + iss[0][1][:40]))
    print()
    print('通过 %d/%d' % (len(cases) - bad, len(cases)))

    # 扁平结构的兼容转换。实测踩过：模型给出 352 字完全正常的内容，
    # 只因为少了一层 sections 数组就被整份丢弃。
    print()
    print('  扁平结构兼容：')
    flat = flatten_to_sections({
        'status': '白细胞 40.79 显著升高。',
        'meaning': '白细胞负责免疫防御。',
        'action': '请立即就医。',
        'unknown_status': '5 项无参考范围，无法判断。',
    })
    extra = [
        ('扁平结构可转 sections', len(flat) == 4, '%d 段' % len(flat)),
        ('status 排在最前', flat and flat[0]['kind'] == 'status'
         and '40.79' in flat[0]['body'], ''),
        ('unknown 段排最后', flat and flat[-1]['kind'] == 'status'
         and '无法判断' in flat[-1]['body'], ''),
        ('未知结构返回空', flatten_to_sections({'foo': 'bar'}) == [], ''),
        ('非 dict 返回空', flatten_to_sections('abc') == [], ''),
    ]
    bad2 = 0
    for name, cond, extra_s in extra:
        if not cond:
            bad2 += 1
        print('%s %-22s %s'
              % ('  OK ' if cond else ' FAIL', name, extra_s))
    print()
    print('扁平兼容 %d/%d' % (len(extra) - bad2, len(extra)))
    return 0 if bad == 0 and bad2 == 0 else 1


def generate_one(rid, items, client=None, model='', base='', key='',
                 mock=False, temperature=0.0,
                 max_tokens=2500, timeout=120, retries=2, debug_raw=False):
    """生成一份解读，返回 (rec, usage)。

    刻意不抛异常：模型调不通、返回不是 JSON、段落全空，都收敛成模板降级。
    调用方据 rec['degraded'] 决定记录成什么，而不是靠 try/except 分散判断。
    """
    try:
        severity = load_grader().grade_report(items)
    except Exception:
        severity = None
    msgs = build_messages(rid, items, severity)
    usage = {}
    degraded = False
    reason = ''
    raw = ''

    if mock:
        raw = json.dumps({'sections': [
            {'kind': 'status', 'title': '本次结果概况',
             'body': '本次共 %d 项，其中 %d 项超出参考范围，%d 项因报告未提供'
                     '参考范围无法判断。'
                     % (len(items),
                        sum(1 for x in items if x.get('is_abnormal') == 'abnormal'),
                        sum(1 for x in items if is_unknown(x)))},
            {'kind': 'meaning', 'title': '这些指标反映什么',
             'body': '这里放模型生成的通俗解释。'},
            {'kind': 'action', 'title': '建议',
             'body': '保持规律作息，必要时携带原始化验单咨询临床医生。'},
        ]}, ensure_ascii=False)
    elif client is None:
        pass                          # 没给客户端，直接走降级
    else:
        try:
            raw, usage = client.call_llm(
                base, model, key, msgs, retries=retries,
                temperature=temperature, max_tokens=max_tokens,
                timeout=timeout)
        except Exception as e:
            print('    调用失败，走降级：%s' % e)
            reason = '调用失败：%s' % str(e)[:120]
            raw, usage = '', {}

    if debug_raw and raw:
        # 现象是"降级了但不知道为什么"，光看 reason 不够时直接看原文：
        # 被 max_tokens 截断的 JSON 结尾会停在半句上，很好认
        with open('_raw_debug.txt', 'a', encoding='utf-8') as fp:
            fp.write('=== %s  %s ===\n%s\n\n'
                     % (rid, time.strftime('%H:%M:%S'), raw))

    parsed = parse_json_reply(raw)
    raw_secs = (parsed or {}).get('sections')
    if not isinstance(raw_secs, list):
        # 模型可能给了扁平结构，先尝试转换再判失败
        raw_secs = flatten_to_sections(parsed)
    # 段数超限不截断后照用，而是整份放弃：逐项罗列不是"格式小问题"，
    # 它意味着模型没按结构写，正文通常也在复述表格
    if len(raw_secs) > MAX_SECTIONS:
        print('    模型输出 %d 段（上限 %d），按结构违规降级'
              % (len(raw_secs), MAX_SECTIONS))
        reason = '输出 %d 段，超过上限 %d（逐项罗列）' % (
            len(raw_secs), MAX_SECTIONS)
        sections = []
    else:
        sections = normalize_llm_sections(raw_secs)
    if not sections:
        degraded = True
        if not reason:
            # 只说"没解析出段落"没法定位问题，必须带上原始返回的形状：
            # 截断的 JSON 和格式正确的 JSON 在开头看不出区别，
            # 但看结尾就知道是被 max_tokens 截了一半还是根本没给 JSON
            head = (raw or '').strip()[:180].replace('\n', ' ')
            tail = (raw or '').strip()[-120:].replace('\n', ' ')
            reason = ('模型未返回可解析段落｜长度 %d 字｜开头：%s｜结尾：%s'
                      % (len(raw or ''), head or '（空）', tail or '（空）'))
        sections = fallback_sections(items)

    return {
        'report_id': rid,
        'revision': 1,
        'sections': sections,
        'disclaimer': DISCLAIMER,     # 强制附加，不采用模型生成的版本
        'degraded': degraded,
        'degrade_reason': reason,
        'model': ('' if mock else model),
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }, usage


def main():
    args = parse_args()
    if args.selftest:
        return selftest()
    if not args.predictions:
        print('需要 --predictions（或用 --selftest）')
        return 1

    rows = [json.loads(l) for l in open(args.predictions, encoding='utf-8')
            if l.strip()]
    # 校验每份解读都要对照它自己的原始指标，所以必须留住完整的一份。
    # 后面 --only / --limit / --resume 都会把 rows 过滤掉，
    # 用过滤后的列表去校验会漏掉没参与本次生成的份。
    all_preds = rows
    if args.only:
        rows = [r for r in rows
                if args.only in (r.get('image') or r.get('report_id') or '')]
        if not rows:
            print('没有报告号包含 %r 的样本' % args.only)
            return 1
    if args.limit:
        rows = rows[:args.limit]

    # 续跑：读已有输出，把成功的份从待跑列表里去掉
    prev_rows = []
    if args.resume and os.path.exists(args.out):
        prev_rows = [json.loads(l)
                     for l in open(args.out, encoding='utf-8') if l.strip()]
        done = {p.get('report_id') for p in prev_rows
                if not p.get('degraded')}
        before = len(rows)
        rows = [r for r in rows
                if (r.get('image') or r.get('report_id')) not in done]
        print('续跑：%s 里已有 %d 份成功，跳过；剩 %d/%d 份待跑'
              % (args.out, len(done), len(rows), before))

    if args.check_file:
        if not os.path.exists(args.check_file):
            print('找不到 %s' % args.check_file)
            return 1
        recs = []
        try:
            for line in open(args.check_file, encoding='utf-8'):
                if line.strip():
                    recs.append(json.loads(line))
        except json.JSONDecodeError as e:
            print('%s 不是合法的解读 jsonl：%s' % (args.check_file, e))
            print('（--check-file 要传 35 输出的 .jsonl，'
                  '不是校验报告 .md）')
            return 1
        print('重新校验 %s（%d 份）' % (args.check_file, len(recs)))
        run_check(recs, all_preds, args.report)
        return 0

    print('待解读 %d 份%s'
          % (len(rows), ('（筛选 %r）' % args.only) if args.only else ''))

    if args.dry_run:
        rid = rows[0].get('image') or rows[0].get('report_id') or '?'
        msgs = build_messages(rid, rows[0].get('items') or [])
        print()
        print('=' * 70)
        print('system prompt')
        print('=' * 70)
        print(msgs[0]['content'])
        print()
        print('=' * 70)
        print('user message')
        print('=' * 70)
        print(msgs[1]['content'])
        return 0

    client = None
    if not args.mock:
        client = load_llm_client()
        base, model, key = client.get_config()
        print('模型: %s' % model)
        print('接口: %s' % base)

    out_rows = []
    t0 = time.time()
    usage_total = {}
    for i, r in enumerate(rows, 1):
        if i > 1 and args.delay > 0:
            time.sleep(args.delay)
        rid = r.get('image') or r.get('report_id') or ('sample_%d' % i)
        items = r.get('items') or []

        print('  [%d/%d] %-38s ' % (i, len(rows), str(rid)[:36]),
              end='', flush=True)
        rec, usage = generate_one(
            rid, items,
            client=client,
            model=(model if client else ''),
            base=(base if client else ''),
            key=(key if client else ''),
            mock=args.mock, temperature=args.temperature,
            max_tokens=args.max_tokens, timeout=args.timeout,
            retries=args.retries, debug_raw=args.debug_raw)
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                usage_total[k] = usage_total.get(k, 0) + v

        out_rows.append(rec)
        print('%d 段%s' % (len(rec['sections']),
                          ('（降级：%s）' % rec['degrade_reason'])
                          if rec['degraded'] else ''))

    if prev_rows:
        merged = {p.get('report_id'): p for p in prev_rows}
        for r in out_rows:
            merged[r.get('report_id')] = r
        out_rows = list(merged.values())

    with open(args.out, 'w', encoding='utf-8') as fp:
        for r in out_rows:
            fp.write(json.dumps(r, ensure_ascii=False) + '\n')

    print()
    print('耗时 %.1f 秒，输出 %s' % (time.time() - t0, args.out))
    if usage_total:
        print('token 用量: %s' % usage_total)
        pin = usage_total.get('prompt_tokens', 0)
        pout = usage_total.get('completion_tokens', 0)
        # DeepSeek 定价：输入约 1 元/百万 token，输出约 2 元/百万 token
        print('估算花费: 约 %.4f 元（按输入1元/输出2元每百万token）'
              % (pin / 1e6 * 1.0 + pout / 1e6 * 2.0))
    n_deg = sum(1 for r in out_rows if r['degraded'])
    if n_deg:
        print('降级 %d 份（模型失败或输出不可解析，已用模板兜底）' % n_deg)

    if args.check:
        # 校验结果不影响退出码，供人工看
        run_check(out_rows, all_preds, args.report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
