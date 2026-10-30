# -*- coding: utf-8 -*-
"""07 本地：按 MedRepBench 官方口径评测 predictions

与官方 evaluate_objective.py 的差异：
  · 只评"有预测"的样本（官方会遍历全部labels，未预测的算0，会虚低）
  · 额外给出 precision / F1（官方只有 recall）
  · 输出逐样本明细，便于定位问题

用法：
    python 07_evaluate.py --labels "<数据目录>/medrepbench/datasets-meta-zhCN.csv"         --predictions predictions.jsonl
    # 或从gold目录读（不需CSV）
    python 07_evaluate.py --gold-dir "<数据目录>/eval_gold_medrep" --predictions predictions.jsonl"""

import os
import sys
import csv
import json
import re
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

FIELDS = ['item_name', 'item_value', 'item_unit', 'item_range', 'is_abnormal']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--labels', default='', help='官方CSV')
    ap.add_argument('--gold-dir', default='', help='或课程版gold目录')
    ap.add_argument('--predictions', required=True)
    ap.add_argument('--include-types', default='Laboratory')
    ap.add_argument('--exclude-unit-missing', action='store_true',
                    help='某份报告的预测项全无单位时，把它的 item_unit 排除出分母。'
                         '用于"OCR 没读出单位列、gold 的单位是人工推断"的情况；'
                         '默认关闭，否则会把解析器漏抽单位也算成"不可评"而掩盖问题。')
    ap.add_argument('--loose', action='store_true',
                    help='额外报告宽松口径：预测的中文名或代码任一命中金标准名称即算命中')
    ap.add_argument('--out', default='eval_result.md')
    return ap.parse_args()


# ---------- 归一化：必须与官方脚本一致 ----------
def normalize_text(value):
    value = '' if value is None else str(value)
    value = value.strip().lower()
    value = value.replace(' ', '')
    value = value.replace('：', ':').replace('－', '-').replace('—', '-').replace('–', '-')
    value = value.replace('~', '-').replace('～', '-')
    # 连续横线合并：'6.4--10.2' 与 '6.4-10.2' 是同一个区间。
    # OCR 不同次运行会在这两种写法间摇摆（实测 090019 的 MPV/PCT、
    # 100123 的 HBDH 都因此被判不一致）。区间语义不依赖横线根数，
    # 合并是安全且有必要的归一。
    value = re.sub(r'-{2,}', '-', value)
    # 单位里的希腊字母与拉丁字母互为等价写法：μmol/L 常写成 umol/L，
    # α/β/γ 也常写成 a/b/g。不归一的话 gold 写 umol/L、预测出 μmol/L
    # 就会被判成不一致（实测因此白丢 3 项）。
    value = (value.replace('\u03bc', 'u').replace('\u00b5', 'u')   # μ µ
                  .replace('\u03b1', 'a').replace('\u03b2', 'b')
                  .replace('\u03b3', 'g'))
    return value


def normalize_number_like(value):
    import re
    value = normalize_text(value)
    value = re.sub(r'(?<=\d)\.0+(?=\D|$)', '', value)
    return value


def field_equal(field, pred, ref):
    if field == 'is_abnormal':
        return abnormal_norm(pred) == abnormal_norm(ref)
    if field in {'item_value', 'item_range'}:
        return normalize_number_like(pred) == normalize_number_like(ref)
    return normalize_text(pred) == normalize_text(ref)


# ---------- 金标准字段适配 ----------
# 官方 CSV 的 items 直接就是 item_name/item_value/... 结构；
# 课程版 eval_gold（05_adapt_medrepbench、23_gold_from_annotation 产出）用的是
# name/value/unit/reference{kind,low,high}/flag。两种都要能评，否则自采数据永远是0%。
def ref_get(ref, field):
    if field == 'item_name':
        return ref.get('item_name') or ref.get('name') or ref.get('indicator_code') or ''
    if field == 'item_value':
        return ref.get('item_value') if ref.get('item_value') is not None else ref.get('value')
    if field == 'item_unit':
        return ref.get('item_unit') if ref.get('item_unit') is not None else ref.get('unit')
    if field == 'item_range':
        if ref.get('item_range') is not None:
            return ref.get('item_range')
        return format_reference(ref.get('reference'))
    if field == 'is_abnormal':
        v = ref.get('is_abnormal')
        if v is None:
            v = ref.get('is_abnormal_gt')
        if v is None:
            v = ref.get('flag')
        return v
    return ref.get(field)


def abnormal_norm(v):
    """把各种来源的异常标记归一成 normal / abnormal / ''（未判）。

    官方 CSV 用 '0'/'1'，课程版 gold 用 'normal'/'abnormal'/'unknown'，
    预测里写 'normal'/'abnormal'/''。不归一的话这条字段永远对不上。
    """
    s = ('' if v is None else str(v)).strip().lower()
    if s in ('1', 'true', 'abnormal', '异常', '阳性', 'high'):
        return 'abnormal'
    if s in ('0', 'false', 'normal', '正常', '阴性'):
        return 'normal'
    return ''


def format_reference(ref):
    """把 reference 对象还原成区间文本，便于逐字比较。

    优先用 raw（标注时原样保留的报告写法，如 '4.4--11.9'）：
    官方金标准的区间分隔符混用 '-'、'--'、'~'，若这里擅自统一成单横线，
    预测明明和报告原文一致也会被判错。
    """
    if not isinstance(ref, dict):
        return ''
    raw = (ref.get('raw') or '').strip()
    if raw:
        return raw
    kind = (ref.get('kind') or '').strip()
    low = ('' if ref.get('low') is None else str(ref.get('low'))).strip()
    high = ('' if ref.get('high') is None else str(ref.get('high'))).strip()
    if kind == 'interval' or (low and high):
        if low and high:
            return f'{low}--{high}'
        return low or high
    if kind == 'text':
        return (ref.get('text') or '').strip()
    return low or high


def gold_item_to_pred_shape(item):
    """把课程版 gold item 转成预测字段名，供 field_equal 直接比较。"""
    return {
        'item_name': ref_get(item, 'item_name'),
        'item_value': ref_get(item, 'item_value'),
        'item_unit': ref_get(item, 'item_unit'),
        'item_range': ref_get(item, 'item_range'),
        'is_abnormal': abnormal_norm(ref_get(item, 'is_abnormal')),
    }


def load_labels_from_csv(path, types):
    labels = {}
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            meta = json.loads(row.get('meta') or '{}')
            if types and meta.get('type') not in types:
                continue
            items = json.loads(row.get('items') or '[]')
            if items:
                labels[row['image']] = items
    return labels


def load_labels_from_gold(gold_dir):
    labels = {}
    for f in glob_json(gold_dir):
        d = json.load(open(f, encoding='utf-8'))
        labels[d['report_id']] = d['items']
    return labels


def glob_json(d):
    import glob
    return [x for x in glob.glob(os.path.join(d, '*.json'))
            if not os.path.basename(x).startswith('_') and
            not os.path.basename(x) == 'manifest.json']


def load_predictions(path):
    preds = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            o = json.loads(line)
            preds[o['image']] = o.get('items') or []
    return preds


def match_items(pred_items, ref_items, loose=False):
    """官方口径：ref 按 item_name 匹配，每条 ref 至多配一条 pred。

    loose=True 时额外允许"预测的代码"去匹配金标准名称，并忽略名称前的
    报告单标记（`*` 自费、`★` 重点项目）。理由：MedRepBench 有相当比例的
    报告单，金标准 item_name 直接写的就是代码（`*WBC`、`LY#`、`P-LCR`），
    中文名在报告里另占一列；只比中文名会把这类报告整份判成未抽出。
    """
    pred_used = set()
    matches = []
    for ref in ref_items:
        ref_name = normalize_text(ref_get(ref, 'item_name'))
        if not ref_name:
            continue
        ref_loose = loose_norm(ref_name)
        for i, pred in enumerate(pred_items):
            if i in pred_used:
                continue
            if normalize_text(pred.get('item_name')) == ref_name:
                pred_used.add(i)
                matches.append((pred, ref))
                break
            if loose and (
                    loose_norm(pred.get('item_name')) == ref_loose
                    or loose_norm(pred.get('item_code')) == ref_loose):
                pred_used.add(i)
                matches.append((pred, ref))
                break
    return matches


MARKER_RE = re.compile(r'^[\*＊★☆#＃]+')


def loose_norm(value):
    """宽松匹配用：去掉报告单标记前缀，其余同官方归一化。"""
    s = '' if value is None else str(value)
    s = MARKER_RE.sub('', s.strip())
    return normalize_text(s)


def main():
    args = parse_args()
    types = {t.strip() for t in args.include_types.split(',') if t.strip()}

    if args.labels:
        labels = load_labels_from_csv(args.labels, types)
    elif args.gold_dir:
        labels = load_labels_from_gold(args.gold_dir)
        # gold 用 report_id 作键，需要映射到 image
        man_path = None
        for n in ('manifest.json', 'upload_manifest.json'):
            p = os.path.join(os.path.dirname(args.gold_dir.rstrip('/\\')) or '.', n)
            if os.path.isfile(p):
                man_path = p
                break
        mp = os.path.join(args.gold_dir, 'manifest.json')
        if not os.path.isfile(mp):
            mp = os.path.join(os.path.dirname(args.gold_dir.rstrip('/\\')), 'upload_manifest.json')
        if os.path.isfile(mp):
            m = json.load(open(mp, encoding='utf-8'))
            mk = {s['report_id']: s['image'] for s in m['samples']}
            labels = {mk.get(k, k): v for k, v in labels.items()}
    else:
        print('需要 --labels 或 --gold-dir')
        sys.exit(1)

    preds = load_predictions(args.predictions)

    # 统一成"不带扩展名、不带 _masked"的报告号再配对。
    # gold 用 report_id（无后缀），预测用图片文件名（常带 _masked.png），
    # 不归一化会整批交集 0 项，表现为"评测结果全是 0%"，很难一眼看出是命名问题。
    def _stem(p):
        s = os.path.splitext(os.path.basename(p or ''))[0]
        return re.sub(r'(_masked|_annot|_已脱敏|_脱敏)$', '', s, flags=re.I)

    labels = {_stem(k): v for k, v in labels.items()}
    preds = {_stem(k): v for k, v in preds.items()}
    evaluated = sorted(set(labels) & set(preds))
    print(f'labels {len(labels)} 份 | predictions {len(preds)} 份 | 交集 {len(evaluated)} 份')
    if not evaluated:
        print('!! 没有可评测样本，检查 predictions 的 image 字段是否与 labels 键一致')
        sys.exit(1)

    totals = collections.Counter()
    correct = collections.Counter()
    l_totals = collections.Counter()
    l_correct = collections.Counter()
    abstain = [0]
    l_abstain = [0]
    unit_missing_samples = []
    pred_total = 0
    per_sample = []

    for img in evaluated:
        ref_items = labels[img]
        pred_items = preds[img]
        pred_total += len(pred_items)
        matches = match_items(pred_items, ref_items)
        # 整份报告所有预测项都没单位 → OCR 没读出单位列。
        # 这时 gold 里的单位是人工按项目常识推断的，不是报告内容，
        # 不该算进解析器的 unit 分母（否则等于用人工的医学知识扣解析器的分）。
        unit_ocr_missing = (args.exclude_unit_missing and bool(pred_items)
                            and all(not (p.get('item_unit') or '').strip()
                                    for p in pred_items))
        if unit_ocr_missing:
            unit_missing_samples.append(len(pred_items))
        for f in FIELDS:
            if f == 'is_abnormal':
                # 分母在下面的匹配循环里按"配对成功且金标准有明确判定"累加，
                # 否则会把名称都没配上的项也算进来，数字没有意义
                continue
            if f == 'item_unit' and unit_ocr_missing:
                continue
            totals[f] += len(ref_items)
            l_totals[f] += len(ref_items)
        correct['item_name'] += len(matches)
        for pred, ref in matches:
            r = gold_item_to_pred_shape(ref)
            for f in FIELDS[1:]:
                if f == 'item_unit' and unit_ocr_missing:
                    continue
                if f == 'is_abnormal':
                    # 单独处理：金标准没判的项不进分子也不进分母
                    gv = abnormal_norm(r.get(f))
                    if not gv:
                        continue
                    totals[f] += 1
                    pv = abnormal_norm(pred.get(f))
                    if pv == gv:
                        correct[f] += 1
                    elif not pv:
                        abstain[0] += 1
                    continue
                if field_equal(f, pred.get(f), r.get(f)):
                    correct[f] += 1
        if args.loose:
            l_matches = match_items(pred_items, ref_items, loose=True)
            l_correct['item_name'] += len(l_matches)
            for pred, ref in l_matches:
                r = gold_item_to_pred_shape(ref)
                for f in FIELDS[1:]:
                    if f == 'item_unit' and unit_ocr_missing:
                        continue
                    if f == 'is_abnormal':
                        gv = abnormal_norm(r.get(f))
                        if not gv:
                            continue
                        l_totals[f] += 1
                        pv = abnormal_norm(pred.get(f))
                        if pv == gv:
                            l_correct[f] += 1
                        elif not pv:
                            l_abstain[0] += 1
                        continue
                    if field_equal(f, pred.get(f), r.get(f)):
                        l_correct[f] += 1
        per_sample.append({
            'image': img, 'gt': len(ref_items), 'pred': len(pred_items),
            'name_hit': len(matches),
        })

    recalls = {f: (correct[f] / totals[f] if totals[f] else 0) for f in FIELDS}
    avg_recall = sum(recalls.values()) / len(FIELDS)
    precision = correct['item_name'] / pred_total if pred_total else 0
    f1 = (2 * precision * recalls['item_name'] / (precision + recalls['item_name'])
          if (precision + recalls['item_name']) else 0)

    print()
    print('=' * 58)
    print('评测结果（MedRepBench 官方口径 recall）')
    print('=' * 58)
    for f in FIELDS:
        print(f'  {f:<12} recall {recalls[f]*100:6.2f}%  ({correct[f]}/{totals[f]})')
    print(f'  {"average":<12} recall {avg_recall*100:6.2f}%'
          f'  （5字段平均；is_abnormal 分母不同，仅供参考）')
    print()
    print(f'  item_name precision {precision*100:.2f}%  (命中{correct["item_name"]}/预测{pred_total})')
    print(f'  item_name F1        {f1*100:.2f}%')

    # is_abnormal 由规则层负责，本阶段留空，故单独给出不含它的抽取均值
    extract_fields = ['item_name', 'item_value', 'item_unit', 'item_range']
    extract_avg = sum(recalls[f] for f in extract_fields) / len(extract_fields)
    print(f'  抽取4字段均值(不含is_abnormal) {extract_avg*100:.2f}%')
    if unit_missing_samples:
        print('  item_unit 分母排除了 %d 份报告（OCR 未读出单位列，共 %d 项）：'
              'gold 里的单位是人工推断的，不算解析器的分'
              % (len(unit_missing_samples), sum(unit_missing_samples)))
    if totals['is_abnormal']:
        ab_t = totals['is_abnormal']
        print(f'  is_abnormal 分母只算金标准有明确判定的 {ab_t} 项；'
              f'其中未判定 {abstain[0]} 项（{'%.1f%%' % (abstain[0]/ab_t*100)}）')

    l_recalls = None
    if args.loose:
        l_recalls = {f: (l_correct[f] / l_totals[f] if l_totals[f] else 0) for f in FIELDS}
        l_avg = sum(l_recalls[f] for f in extract_fields) / len(extract_fields)
        print()
        print('-' * 58)
        print('宽松口径（预测的中文名或代码任一命中即算命中，去 * ★ 标记）')
        print('-' * 58)
        for f in FIELDS:
            print(f'  {f:<12} recall {l_recalls[f]*100:6.2f}%  '
                  f'({l_correct[f]}/{l_totals[f]})')
        print(f'  抽取4字段均值(不含is_abnormal) {l_avg*100:.2f}%')
        print(f'  相对严格口径 item_name 提升 '
              f'{(l_recalls["item_name"]-recalls["item_name"])*100:+.2f} 个百分点')

    # 明细
    with open(args.out, 'w', encoding='utf-8') as fo:
        fo.write('# 抽取评测结果\n\n')
        fo.write('## 汇总\n\n| 字段 | recall | 命中/总数 |\n| --- | --- | --- |\n')
        for f in FIELDS:
            fo.write(f'| {f} | {recalls[f]*100:.2f}% | {correct[f]}/{totals[f]} |\n')
        fo.write(f'| **average** | **{avg_recall*100:.2f}%** | |\n\n')
        fo.write(f'- item_name precision: {precision*100:.2f}% '
                 f'(命中{correct["item_name"]}/预测{pred_total})\n')
        fo.write(f'- item_name F1: {f1*100:.2f}%\n\n')
        if l_recalls:
            fo.write('## 宽松口径（中文名或代码任一命中）\n\n')
            fo.write('| 字段 | recall | 命中/总数 |\n| --- | --- | --- |\n')
            for f in FIELDS:
                fo.write(f'| {f} | {l_recalls[f]*100:.2f}% | '
                         f'{l_correct[f]}/{l_totals[f]} |\n')
            fo.write('\n')
        fo.write('## 逐样本\n\n| image | GT项数 | 预测项数 | 名称命中 |\n| --- | --- | --- | --- |\n')
        for s in per_sample:
            fo.write(f'| {s["image"]} | {s["gt"]} | {s["pred"]} | {s["name_hit"]} |\n')
    print(f'\n明细: {args.out}')


if __name__ == '__main__':
    main()
