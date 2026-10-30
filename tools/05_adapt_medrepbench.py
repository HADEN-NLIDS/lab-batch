# -*- coding: utf-8 -*-
"""MedRepBench -> 课程版 gold 格式适配器

设计原则（避免循环论证）：
  · item_name/value/unit/range 用 MedRepBench 官方人工标注，不改动
  · is_abnormal 原样保留，作为"异常判定"的独立金标准
  · triage / indicator_code / is_critical 不臆造，留空待规则层单独评测

用法：
  python medrepbench_adapter.py
  python medrepbench_adapter.py --src "<数据目录>/medrepbench" --out "<数据目录>/eval_gold_medrep" --min-items 3

历史工具：为公开数据集搭评测闭环时编写。该数据集脱敏较重、版面结构受损，
识别效果不具代表性，当前测试以真实报告单脱敏为主，此脚本保留作参考。
"""

import argparse
import csv
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

DASHES = ['－', '—', '–', '~', '～', '〜']
NUM = r'[-+]?\d+(?:\.\d+)?'


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--src', default=r'<数据目录>/medrepbench')
    ap.add_argument('--out', default=r'<数据目录>/eval_gold_medrep')
    ap.add_argument('--min-items', type=int, default=3,
                    help='最少项目数，低于此值不纳入（默认3）')
    ap.add_argument('--max-samples', type=int, default=0,
                    help='最多转几份，0表示不限制')
    ap.add_argument('--no-usable-list', action='store_true',
                    help='不套用官方 usable_list.csv 白名单')
    ap.add_argument('--include-types', default='Laboratory',
                    help='逗号分隔的meta.type，默认只要化验单')
    return ap.parse_args()


def norm_dash(s):
    for d in DASHES:
        s = s.replace(d, '-')
    return s


def parse_reference(raw):
    """把 '9-14' / '<5.2' / '阴性' 解析成结构化 reference。

    raw 一定要保留原样：官方金标准的区间分隔符混用 '-'（2497）、'--'（666）、
    '~'（568），如果这里丢掉原文、评测时再拼回去，预测即使和报告原文一致也会被判错。
    """
    s = '' if raw is None else str(raw).strip()
    if not s:
        return {'kind': 'missing', 'raw': ''}
    # 纯符号（-, --, ~, /, 空格等）实际等于未填写，归 missing 而非 text
    if re.fullmatch(r'[-－—–~～\s./|、]+', s):
        return {'kind': 'missing', 'raw': s}
    s2 = norm_dash(s).replace(' ', '')
    s2 = s2.replace('＜', '<').replace('＞', '>').replace('≤', '<').replace('≥', '>')

    # 单侧
    m = re.fullmatch(r'[<]\s*(' + NUM + r')', s2)
    if m:
        return {'kind': 'one_sided', 'high': m.group(1), 'raw': s}
    m = re.fullmatch(r'[>]\s*(' + NUM + r')', s2)
    if m:
        return {'kind': 'one_sided', 'low': m.group(1), 'raw': s}

    # 区间：MedRepBench 用双横线表示区间（9.4--12.5、80--160），必须优先匹配，
    # 否则会被单横线规则吃掉一个 '-' 当作负号，得到 high=-12.5 这种错误结果
    m = re.fullmatch(r'(' + NUM + r')--(' + NUM + r')', s2)
    if m:
        return {'kind': 'interval', 'low': m.group(1), 'high': m.group(2), 'raw': s}

    # 单横线区间（9-14、3.9-6.1）
    m = re.fullmatch(r'(' + NUM + r')-(' + NUM + r')', s2)
    if m:
        return {'kind': 'interval', 'low': m.group(1), 'high': m.group(2), 'raw': s}

    # 非数值（阴性/阳性/未见异常等）
    return {'kind': 'text', 'raw': s}


def parse_value(raw):
    """返回 (数值字符串, 运算符)。'<0.1' -> ('0.1','lt')。"""
    s = '' if raw is None else str(raw).strip()
    if not s:
        return '', 'missing'
    for sym, op in (('<', 'lt'), ('>', 'gt'), ('＜', 'lt'), ('＞', 'gt')):
        if s.startswith(sym):
            return s[1:].strip(), op
    return s, 'eq'


def flag_from_abnormal(raw):
    """MedRepBench的is_abnormal是二值，只能确定normal还是异常，无法分高低。"""
    v = str(raw).strip()
    if v in ('0', '0.0', 'false', 'False', '正常'):
        return 'normal'
    if v in ('1', '1.0', 'true', 'True', '异常'):
        return 'abnormal'   # 注意：不是high/low，是新引入的中间值
    return 'unknown'


def load_usable(src):
    """读取官方 usable_list.csv 作为白名单。
    实测该表 686 条全部为有效文件（0字节0个、缺失0个），是官方筛过的干净子集。"""
    path = os.path.join(src, 'usable_list.csv')
    if not os.path.isfile(path):
        return None
    with open(path, newline='', encoding='utf-8-sig') as f:
        return {r.get('image') for r in csv.DictReader(f) if r.get('image')}


def main():
    args = parse_args()
    csv_path = os.path.join(args.src, 'datasets-meta-zhCN.csv')
    if not os.path.isfile(csv_path):
        print('找不到元数据文件:', csv_path)
        sys.exit(1)

    types = {t.strip() for t in args.include_types.split(',') if t.strip()}
    os.makedirs(args.out, exist_ok=True)

    usable = None if args.no_usable_list else load_usable(args.src)
    if usable is not None:
        print(f'已加载官方白名单 usable_list.csv：{len(usable)} 条')

    stats = {
        'total_rows': 0,
        'skipped_type': 0,
        'skipped_empty_items': 0,
        'skipped_min_items': 0,
        'skipped_not_usable': 0,
        'skipped_empty_file': 0,
        'written': 0,
        'ref_kinds': {},
        'flags': {},
        'item_total': 0,
    }

    manifest = []
    img_dir = os.path.join(args.src, 'censored')

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            stats['total_rows'] += 1
            meta = json.loads(row.get('meta') or '{}')
            if types and meta.get('type') not in types:
                stats['skipped_type'] += 1
                continue

            image = row['image']
            if usable is not None and image not in usable:
                stats['skipped_not_usable'] += 1
                continue

            # 双重校验：文件存在且非空（原数据集有387个0字节文件，占20%）
            fp = os.path.join(img_dir, image)
            if not os.path.isfile(fp) or os.path.getsize(fp) == 0:
                stats['skipped_empty_file'] += 1
                continue

            items_raw = json.loads(row.get('items') or '[]')
            if not items_raw:
                stats['skipped_empty_items'] += 1
                continue
            if len(items_raw) < args.min_items:
                stats['skipped_min_items'] += 1
                continue

            gold_items = []
            for it in items_raw:
                val, op = parse_value(it.get('item_value'))
                ref = parse_reference(it.get('item_range'))
                flag = flag_from_abnormal(it.get('is_abnormal'))
                stats['ref_kinds'][ref['kind']] = stats['ref_kinds'].get(ref['kind'], 0) + 1
                stats['flags'][flag] = stats['flags'].get(flag, 0) + 1
                gold_items.append({
                    'name': str(it.get('item_name', '')).strip(),
                    'indicator_code': None,        # 不臆造，规则层另行映射
                    'value': val,
                    'value_operator': op,
                    'unit': str(it.get('item_unit', '')).strip(),
                    'reference': ref,
                    'flag': flag,
                    'is_abnormal_gt': str(it.get('is_abnormal', '')).strip(),
                    'triage': None,                # 不臆造
                })
            stats['item_total'] += len(gold_items)

            report_id = os.path.splitext(image)[0]
            doc = {
                'report_id': report_id,
                'revision': 1,
                'expected_triage': None,
                'expected_completeness': 'full',
                'source': 'MedRepBench (CC BY-NC 4.0)',
                'meta': meta,
                'note': ('由 MedRepBench 官方标注转换。flag 由 is_abnormal 二值标注得出，'
                         'abnormal 不分高低；triage/indicator_code 留空，不参与规则层评测，'
                         '避免用规则生成的金标准反向评测规则。'),
                'items': gold_items,
            }
            with open(os.path.join(args.out, report_id + '.json'), 'w',
                      encoding='utf-8') as fp:
                json.dump(doc, fp, ensure_ascii=False, indent=2)

            manifest.append({
                'report_id': report_id,
                'image': image,
                'num_items': len(gold_items),
                'meta_type': meta.get('type'),
            })
            stats['written'] += 1
            if args.max_samples and stats['written'] >= args.max_samples:
                break

    with open(os.path.join(args.out, 'manifest.json'), 'w', encoding='utf-8') as fp:
        json.dump({'count': len(manifest), 'samples': manifest}, fp,
                  ensure_ascii=False, indent=2)

    print('=' * 58)
    print('转换完成')
    print('=' * 58)
    print(f'源文件       : {csv_path}')
    print(f'输出目录     : {args.out}')
    print(f'总行数       : {stats["total_rows"]}')
    print(f'主要跳过原因：')
    print(f'  类型不符   : {stats["skipped_type"]}')
    print(f'  不在白名单 : {stats["skipped_not_usable"]}')
    print(f'  空文件     : {stats["skipped_empty_file"]}')
    print(f'  无项目     : {stats["skipped_empty_items"]}')
    print(f'  少于{args.min_items}项    : {stats["skipped_min_items"]}')
    print(f'实际写出     : {stats["written"]} 份，共 {stats["item_total"]} 个项目')
    print()
    print('reference 类型分布:', stats['ref_kinds'])
    print('flag 分布        :', stats['flags'])
    print()
    if stats['written']:
        print(f'平均每份项目数: {stats["item_total"] / stats["written"]:.1f}')
    print()
    print('下一步：')
    print('  1. 用 manifest.json 里的样本跑 OCR，产出 predictions.jsonl')
    print('  2. 用 MedRepBench 官方脚本评测字段 recall：')
    print(f'     python "{os.path.join(args.src, "scripts", "evaluate_objective.py")}" \\')
    print(f'       --labels "{csv_path}" --predictions predictions.jsonl')


if __name__ == '__main__':
    main()
