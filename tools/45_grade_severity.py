"""45_grade_severity.py — 异常严重度分级

按危急值和偏离幅度给每个异常项定级（critical / marked / moderate / mild），
结果作为既定事实传给解读模型，由模型决定措辞，不自行判断紧迫性。

用法：
  python tools/45_grade_severity.py --predictions p.jsonl [--out graded.jsonl]
  python tools/45_grade_severity.py --selftest
"""

import argparse
import importlib.util
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))


def load_mod(fname, alias):
    spec = importlib.util.spec_from_file_location(alias,
                                                  os.path.join(HERE, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m34 = load_mod('34_store.py', 'm34')

LEVELS = ('critical', 'marked', 'moderate', 'mild', 'normal', 'unknown',
          'ungraded')

# 危急值，格式 (关键词, 下限, 上限, 排除词)。
# 数值仅为原型验证用，实际部署须按机构 SOP 替换。
# 排除词用于避免衍生指标借用主指标阈值，如"平均血红蛋白浓度"含"血红蛋白"。
CRITICAL = [
    ('白细胞计数', 1.0, 30.0, ('分类', '百分比', '比率', '绝对值')),
    ('血红蛋白', 60.0, 200.0, ('平均', '浓度', '糖化', '胎儿', '含量')),
    ('血小板计数', 20.0, 1000.0, ('压积', '体积', '分布', '平均')),
    ('红细胞压积', None, 60.0, ('平均',)),
    ('钾', 2.5, 6.5, ('尿', 'mmol')),
    ('钠', 120.0, 160.0, ('尿',)),
    ('血糖', 2.5, 25.0, ('糖化', '平均')),
    ('肌酐', None, 500.0, ('清除率', '比值')),
    ('丙氨酸氨基转移酶', None, 500.0, ()),
    ('天门冬氨酸氨基转移酶', None, 500.0, ()),
    ('总胆红素', None, 200.0, ('直接', '间接')),
    ('淀粉酶', None, 500.0, ('尿',)),
    ('肌钙蛋白', None, 1.0, ()),
]


# 无参考范围、但数值本身即具警示意义的指标（阈值须经专业确认）。
ABSOLUTE_ALERT = [
    ('原始幼稚细胞', 5.0, '外周血出现原始/幼稚细胞即需评估'),
    ('原始细胞', 5.0, '外周血出现原始细胞即需评估'),
    ('幼稚细胞', 5.0, '外周血出现幼稚细胞即需评估'),
    ('未成熟粒细胞', 5.0, '未成熟粒细胞比例升高需评估'),
]

# 文本型结果同样可能是警示信号，无法用数值阈值判断
TEXT_ALERT = [
    ('见人工镜检', 'moderate', '报告提示需人工镜检，通常意味存在需医生确认的异常细胞'),
    ('人工分类', 'mild', '报告采用人工分类，建议由医生结合镜检结果解读'),
]


def absolute_alert(name, value):
    """按绝对值判断警示等级。返回 (level, reason) 或 None。"""
    nm = name or ''
    if value is None:
        return None
    for kw, thr, note in ABSOLUTE_ALERT:
        if kw in nm and value >= thr:
            return 'critical', '数值 %g 已达 %s 的预警阈值 %g：%s' % (
                value, kw, thr, note)
    return None


def text_alert(name, raw_value):
    """按文本型结果判断警示等级。返回 (level, reason) 或 None。"""
    nm, rv = (name or ''), (raw_value or '')
    for kw, lv, note in TEXT_ALERT:
        if kw in rv or kw in nm:
            return lv, '%s：%s' % (rv or nm, note)
    return None


def critical_of(name):
    """按名称找危急值区间。

    两条约束缺一不可：
      · 长名优先，避免'白细胞'抢了'白细胞计数'
      · 排除词，避免衍生指标借用主指标阈值
        （'平均血红蛋白浓度'含'血红蛋白'，但两者量纲含义不同）
    """
    nm = (name or '')
    best = None
    for kw, lo, hi, excl in CRITICAL:
        if kw not in nm:
            continue
        if any(e in nm for e in excl):
            continue
        if best is None or len(kw) > len(best[0]):
            best = (kw, lo, hi)
    return best


def grade_one(it):
    """给单项定级，返回 (level, reason)。

    level ∈ normal / mild / moderate / marked / critical / unknown / ungraded
    reason 是给人看的一句话依据，审计时要用。
    """
    flag = it.get('is_abnormal') or ''
    nm = it.get('item_name') or ''
    v = m34.to_num(it.get('item_value'))

    # 绝对值和文本预警排在 flag 之前：没有参考范围不等于不严重，
    # "原始幼稚细胞 77%" 就是因为走到 unknown 分支被漏掉的
    aa = absolute_alert(nm, v)
    if aa:
        return aa
    if flag == 'normal':
        return 'normal', ''
    ta = text_alert(nm, it.get('item_value'))
    if ta and flag not in ('normal',):
        return ta

    if flag not in ('normal', 'abnormal'):
        return 'unknown', '报告未提供参考范围，无法判断'
    kind, lo, hi = m34.ref_bounds(it.get('item_range'))
    raw = (it.get('item_range') or '').strip()

    if v is None:
        return 'ungraded', '异常但数值无法解析（值=%r）' % (
            it.get('item_value'))

    # ① 危急值优先：触及即最高级，不再看偏离幅度
    cv = critical_of(nm)
    if cv:
        _kw, clo, chi = cv
        if clo is not None and v < clo:
            return 'critical', '低于危急值 %g（参考范围 %s）' % (clo, raw)
        if chi is not None and v > chi:
            return 'critical', '高于危急值 %g（参考范围 %s）' % (chi, raw)

    # ② 偏离幅度
    # 分母的选择要看区间宽窄：
    # 像 HDL 这种区间只有 0.26 宽的指标，偏低 0.27 就"超过一倍区间宽度"，
    # 但相对界限只差 23%，临床上只是轻度。所以窄区间用相对变化率，
    # 宽区间用区间比例——否则窄区间指标会被系统性高估。
    if kind == 'interval' and lo is not None and hi is not None:
        w = hi - lo
        if w <= 0:
            return 'ungraded', '参考区间宽度无效（%s）' % raw
        if v > hi:
            bound, side = hi, '高'
        elif v < lo:
            bound, side = lo, '低'
        else:
            return 'normal', ''
        narrow = hi > 0 and (w / hi) < 0.2
        if narrow and bound:
            dev = abs(v - bound) / abs(bound)
            basis = '相对界限 %.2f 倍' % dev
        else:
            dev = abs(v - bound) / w
            basis = '区间宽度的 %.2f 倍' % dev
    elif kind == 'one_sided_high' and hi:
        if v <= hi:
            return 'normal', ''
        bound, side = hi, '高'
        dev = abs(v - hi) / abs(hi)
        basis = '相对界限 %.2f 倍' % dev
    elif kind == 'one_sided_low' and lo:
        if v >= lo:
            return 'normal', ''
        bound, side = lo, '低'
        dev = abs(lo - v) / abs(lo)
        basis = '相对界限 %.2f 倍' % dev
    else:
        return 'ungraded', '参考范围形态不支持定量分级（%s）' % raw

    if dev >= 1.0:
        lv = 'marked'
    elif dev >= 0.3:
        lv = 'moderate'
    else:
        lv = 'mild'
    return lv, '%s于参考范围，偏离%s' % (side, basis)


def grade_report(items):
    """给整份报告定级，返回结构化结果。

    max_level 取所有项里最重的一级——解读的紧迫性措辞由它决定。
    """
    rows = []
    counts = {k: 0 for k in LEVELS}
    for it in items or []:
        lv, why = grade_one(it)
        counts[lv] = counts.get(lv, 0) + 1
        if lv in ('normal', 'unknown'):
            continue
        rows.append({
            'name': it.get('item_name') or '',
            'value': it.get('item_value') or '',
            'unit': it.get('item_unit') or '',
            'range': it.get('item_range') or '',
            'level': lv,
            'reason': why,
        })

    order = {k: i for i, k in enumerate(LEVELS)}
    worst = 'normal'
    for r in rows:
        if order.get(r['level'], 99) < order.get(worst, 99):
            worst = r['level']

    return {
        'max_level': worst,
        'counts': counts,
        'n_abnormal': len(rows),
        'items': rows,
    }


# 分级对应的紧迫性要求。解读必须与它一致，校验器会查。
URGENCY = {
    'critical': '立即就医（明显不适时急诊）',
    'marked': '尽快就诊（建议数日内）',
    'moderate': '近期就诊查明原因',
    'mild': '可先复查，持续或加重再就诊',
    'normal': '维持，按常规周期复查',
}


def selftest():
    ok = [0]
    bad = [0]

    def chk(name, cond, extra=''):
        if cond:
            ok[0] += 1
            print('  OK  %-38s %s' % (name, extra))
        else:
            bad[0] += 1
            print('  FAIL %-38s %s' % (name, extra))

    def item(nm, v, rg, flag='abnormal'):
        return {'item_name': nm, 'item_value': v, 'item_unit': '',
                'item_range': rg, 'is_abnormal': flag}

    # 危急值优先于偏离幅度
    lv, why = grade_one(item('白细胞计数(WBC)', '40.79', '4.4--11.9'))
    chk('危急值判 critical', lv == 'critical', '%s / %s' % (lv, why))

    # 同一项目未到危急值，但偏离很大
    lv, _ = grade_one(item('白细胞计数(WBC)', '20', '4--10'))
    chk('偏离 1.67 倍判 marked', lv == 'marked', lv)

    # 轻度偏离
    lv, _ = grade_one(item('红细胞压积 (HCT)', '48.1', '37-48'))
    chk('超出 0.01 倍判 mild', lv == 'mild', lv)

    # 中度
    lv, _ = grade_one(item('甘油三酯', '2.3', '0.4-1.7'))
    chk('偏离 0.38 倍判 moderate', lv == 'moderate', lv)

    # 各项边界
    chk('正常项判 normal',
        grade_one(item('血红蛋白', '145', '130-175', 'normal'))[0]
        == 'normal')
    chk('无参考范围判 unknown',
        grade_one(item('DeRitis比值', '1.1', '', 'unknown'))[0] == 'unknown')

    # 侧限区间
    lv, _ = grade_one(item('C反应蛋白', '50', '<10'))
    chk('单侧上限可分级', lv in ('marked', 'critical'), lv)

    # 整份取最重
    rep = grade_report([
        item('白细胞计数(WBC)', '40.79', '4.4--11.9'),
        item('红细胞压积 (HCT)', '48.1', '37-48'),
        item('血红蛋白', '145', '130-175', 'normal'),
    ])
    chk('整份取最重级', rep['max_level'] == 'critical', rep['max_level'])
    chk('异常项计数', rep['n_abnormal'] == 2, str(rep['n_abnormal']))
    chk('正常项不入异常清单',
        all(r['name'] != '血红蛋白' for r in rep['items']))

    # 危急值关键词不能被短名抢走
    chk('长名优先', critical_of('白细胞计数(WBC)')[0] == '白细胞计数')
    chk('无匹配返回 None', critical_of('身高') is None)

    # 衍生指标不能借用主指标的危急值阈值
    chk('平均血红蛋白浓度不取血红蛋白危急值',
        critical_of('平均血红蛋白浓度') is None,
        str(critical_of('平均血红蛋白浓度')))
    lv, why = grade_one(item('平均血红蛋白浓度', '289', '316--354'))
    chk('MCHC 289 是偏低而非危急', lv in ('mild', 'moderate'), '%s / %s' % (lv, why))

    # 窄区间指标不能被区间比例系统性放大
    lv, why = grade_one(item('高密度脂蛋白胆固醇', '0.89', '1.16-1.42'))
    chk('HDL 0.89 判 mild 而非 marked', lv == 'mild', '%s / %s' % (lv, why))

    # 全正常
    rep2 = grade_report([item('血红蛋白', '145', '130-175', 'normal')])
    chk('全正常 max_level=normal', rep2['max_level'] == 'normal')

    # 无参考范围但绝对值危险：不能因为 flag=unknown 就漏掉
    lv, why = grade_one(item('原始幼稚细胞', '77.0', '', 'unknown'))
    chk('原始细胞 77% 判 critical 而非 unknown', lv == 'critical',
        '%s / %s' % (lv, why))
    rep3 = grade_report([
        item('原始幼稚细胞', '77.0', '', 'unknown'),
        item('血红蛋白', '145', '130-175', 'normal'),
    ])
    chk('只有该项异常时整份仍是 critical',
        rep3['max_level'] == 'critical', rep3['max_level'])

    # 比例不高时不误报
    lv, _ = grade_one(item('原始幼稚细胞', '1.0', '', 'unknown'))
    chk('原始细胞 1% 不判 critical', lv == 'unknown', lv)

    # 文本型结果
    lv, why = grade_one(item('异常白细胞', '见人工镜检', '', 'unknown'))
    chk('见人工镜检给出警示', lv == 'moderate', '%s / %s' % (lv, why))

    print()
    print('通过 %d/%d' % (ok[0], ok[0] + bad[0]))
    return 0 if bad[0] == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--predictions', default='')
    ap.add_argument('--out', default='')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.predictions:
        print('需要 --predictions（或用 --selftest）')
        return 1

    rows = [json.loads(l) for l in open(args.predictions, encoding='utf-8')
            if l.strip()]
    if args.limit:
        rows = rows[:args.limit]

    out = []
    n_by = {}
    for r in rows:
        rid = r.get('report_id') or r.get('image') or ''
        rep = grade_report(r.get('items') or [])
        n_by[rep['max_level']] = n_by.get(rep['max_level'], 0) + 1
        out.append({'report_id': rid, 'severity': rep})
        print('%s  %s（异常 %d 项，%s）'
              % (rid[:40], rep['max_level'].upper(), rep['n_abnormal'],
                 '、'.join('%s %d' % (k, v)
                          for k, v in rep['counts'].items() if v)))
        for it in rep['items']:
            print('    [%-8s] %s %s %s  ← %s'
                  % (it['level'], it['name'], it['value'], it['unit'],
                     it['reason']))

    print()
    print('分级分布：%s' % '、'.join('%s %d' % (k, v)
                                    for k, v in sorted(n_by.items())))
    print()
    print('对应紧迫性：')
    for r in out:
        lv = r['severity']['max_level']
        print('  %-40s %s' % (r['report_id'][:38], URGENCY.get(lv, '')))

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fp:
            for r in out:
                fp.write(json.dumps(r, ensure_ascii=False) + '\n')
        print()
        print('已写出 %s' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
