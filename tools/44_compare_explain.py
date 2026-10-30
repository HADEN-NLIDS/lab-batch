r"""44_compare_explain.py — 对比两个模型的解读产出

同一批报告分别用两个模型跑 35_llm_explain.py，把两份结果并排出来。

用法：
  python tools/44_compare_explain.py --a qwen.jsonl --b glm.jsonl \
      --predictions predictions_self_flagged.jsonl --out 模型对比.md"""

import argparse
import json
import sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')

KINDS = ('status', 'meaning', 'action')


def load_jsonl(path):
    out = []
    with open(path, encoding='utf-8') as fp:
        for line in fp:
            if line.strip():
                out.append(json.loads(line))
    return out


def body_len(rec):
    return sum(len((s or {}).get('body') or '') for s in rec.get('sections') or [])


def kind_set(rec):
    return {(s or {}).get('kind') for s in rec.get('sections') or []}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--a', required=True, help='第一个模型的 jsonl')
    ap.add_argument('--b', required=True, help='第二个模型的 jsonl')
    ap.add_argument('--predictions', default='',
                    help='原始预测 jsonl，给了才能跑自动校验')
    ap.add_argument('--out', default='模型对比.md')
    ap.add_argument('--label-a', default='')
    ap.add_argument('--label-b', default='')
    args = ap.parse_args()

    A = {r['report_id']: r for r in load_jsonl(args.a)}
    B = {r['report_id']: r for r in load_jsonl(args.b)}
    la = args.label_a or (next(iter(A.values())).get('model') or 'A')
    lb = args.label_b or (next(iter(B.values())).get('model') or 'B')

    only_a = sorted(set(A) - set(B))
    only_b = sorted(set(B) - set(A))
    common = [k for k in A if k in B]

    # 校验需要原始 items
    src = {}
    checker = None
    if args.predictions:
        import importlib.util
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            'm35', os.path.join(here, '35_llm_explain.py'))
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        for r in load_jsonl(args.predictions):
            k = r.get('image') or r.get('report_id')
            if k:
                src[k] = r.get('items') or []

    def issues_of(rec):
        if not checker:
            return []
        return checker.check_explanation(rec, src.get(rec['report_id']) or [])

    lines = []
    def w(s=''):
        lines.append(s)

    w('# 解读模型对比：%s vs %s' % (la, lb))
    w()
    w('- 报告数：%s %d 份，%s %d 份，交集 %d 份'
      % (la, len(A), lb, len(B), len(common)))
    if only_a:
        w('- 只在 %s 里：%s' % (la, '、'.join(only_a)))
    if only_b:
        w('- 只在 %s 里：%s' % (lb, '、'.join(only_b)))
    w()

    # ---- 汇总表 ----
    w('## 一、汇总')
    w()
    w('| 报告 | %s 段数 | %s 字数 | %s 段数 | %s 字数 | 结构 |'
      % (la, la, lb, lb))
    w('| --- | --- | --- | --- | --- | --- |')
    tot = {la: [0, 0, 0], lb: [0, 0, 0]}
    for k in common:
        a, b = A[k], B[k]
        ka, kb = kind_set(a), kind_set(b)
        mark = '' if ka == kb else '不同'
        w('| %s | %d | %d | %d | %d | %s |'
          % (k, len(a.get('sections') or []), body_len(a),
             len(b.get('sections') or []), body_len(b), mark))
        for lab, rec in ((la, a), (lb, b)):
            tot[lab][0] += 1
            tot[lab][1] += len(rec.get('sections') or [])
            tot[lab][2] += body_len(rec)
    w()
    for lab, recs in ((la, A), (lb, B)):
        n = len(recs) or 1
        w('- **%s**：平均 %.1f 段，平均 %d 字'
          % (lab, tot[lab][1] / n, tot[lab][2] / max(tot[lab][0], 1)))
        kinds = Counter()
        for r in recs.values():
            kinds.update(kind_set(r))
        w('  - 段落类型：%s'
          % '、'.join('%s %d' % (k or '空', v)
                     for k, v in sorted(kinds.items(), key=lambda x: str(x[0]))))
        missing = [k for k in KINDS if kinds.get(k, 0) < len(recs)]
        if missing:
            w('  - 不是每份都有：%s' % '、'.join(missing))
    w()

    # ---- 校验 ----
    if checker:
        w('## 二、自动校验')
        w()
        w('| 报告 | %s 问题 | %s 问题 |' % (la, lb))
        w('| --- | --- | --- |')
        bad = {la: 0, lb: 0}
        detail = []
        for k in common:
            ia, ib = issues_of(A[k]), issues_of(B[k])
            if ia:
                bad[la] += 1
            if ib:
                bad[lb] += 1
            w('| %s | %d | %d |' % (k, len(ia), len(ib)))
            for lab, iss in ((la, ia), (lb, ib)):
                for kind, msg in iss:
                    detail.append((k, lab, kind, msg))
        w()
        w('- %s：%d/%d 份有问题' % (la, bad[la], len(common)))
        w('- %s：%d/%d 份有问题' % (lb, bad[lb], len(common)))
        w()
        if detail:
            w('### 问题明细')
            w()
            for k, lab, kind, msg in detail:
                w('- `%s` **%s** [%s] %s' % (k, lab, kind, msg))
            w()
            kinds = Counter(k for _, _, k, _ in detail)
            w('问题类型分布：%s'
              % '、'.join('%s %d' % (k, v) for k, v in kinds.items()))
            w()
        else:
            w('两个模型都没有触发校验规则。')
            w()
        # 提示：问题集中在一类时该怎么改提示词
        if detail:
            top = Counter(k for _, _, k, _ in detail).most_common(1)[0][0]
            if top == '越界':
                w('> 越界类问题占多数，说明提示词对"不要下诊断/不要提用药"的约束'
                  '还不够硬。可以在 system prompt 里把这一条挪到最前面，'
                  '并给一个反例。')
            elif top == '事实':
                w('> 事实类问题占多数，通常是模型自行补了参考范围或臆测数值。'
                  '检查 prompt 里"只能引用给定数值"的位置和措辞。')
            elif top == '结构':
                w('> 结构类问题说明段落类型没按格式给。'
                  '把 json 模板贴进 prompt，并明确"必须包含 status 段"。')
            w()

    # ---- 逐份并排 ----
    w('## 三、逐份对照')
    w()
    for k in common:
        w('### %s' % k)
        w()
        for lab, rec in ((la, A[k]), (lb, B[k])):
            w('**%s**%s' % (lab, ('（降级：%s）' % rec.get('degrade_reason'))
                            if rec.get('degraded') else ''))
            w()
            for s in rec.get('sections') or []:
                w('- `[%s]` **%s**' % (s.get('kind') or '?', s.get('title') or ''))
                for ln in (s.get('body') or '').splitlines():
                    w('  > %s' % ln)
            w()

    text = '\n'.join(lines)
    with open(args.out, 'w', encoding='utf-8') as fp:
        fp.write(text)
    print('对比报告: %s' % args.out)
    print()
    print('汇总：%s 平均 %.1f 段，%s 平均 %.1f 段'
          % (la, tot[la][1] / max(tot[la][0], 1),
             lb, tot[lb][1] / max(tot[lb][0], 1)))
    if checker:
        print('%s %d/%d 份有问题，%s %d/%d 份有问题'
              % (la, bad[la], len(common), lb, bad[lb], len(common)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
