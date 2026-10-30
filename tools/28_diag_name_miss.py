# -*- coding: utf-8 -*-
r"""28 本地：拆解 item_name 未命中的原因，回答"继续优化识图还有多少空间"

为什么需要：
    07_evaluate.py 只给一个总 recall。但 recall 低可能来自三种完全不同的原因，
    对应的投入方向也完全不同：
      1. 名字根本没被 OCR 读到（打码 / 图太糊）→ 换模型才有用，但数据上限锁死
      2. 名字读到了，但写法与金标准不同（'γ-谷氨酰转肽酶' vs 'gamma-谷氨酰基转肽酶'）
         → 加别名归一化就有用，不需要动 OCR
      3. 表格结构没解析出来（双栏、跨页、无表格线）
         → 需要结构化理解，OCR 再准也没用

用法：
    python tools\28_diag_name_miss.py --artifacts .\artifacts ^
        --gold "<数据目录>/eval_gold_medrep" --predictions predictions.jsonl"""

import os
import re
import sys
import json
import glob
import difflib
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

# 复用 21 的可见性判定与归一化，保证口径一致
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
import importlib.util


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v21 = _load('visible21', os.path.join(_here, '21_visible_recall.py'))
ev7 = _load('eval07', os.path.join(_here, '07_evaluate.py'))

normalize_text = v21.normalize_text
ref_name = v21.ref_name
visibility = v21.visibility
ocr_fulltext = v21.ocr_fulltext


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', required=True)
    ap.add_argument('--gold', required=True)
    ap.add_argument('--predictions', required=True)
    ap.add_argument('--near', type=float, default=0.7,
                    help='相似度≥该值判为"写法差异"（默认0.7）')
    ap.add_argument('--loose', action='store_true',
                    help='与 07 的宽松口径一致：预测的代码也参与匹配')
    ap.add_argument('--out', default='name_miss_diag.md')
    return ap.parse_args()


def simplify(name):
    """去掉括号内容和缩写，只留中文主干，用于判断"是不是同一个项目"。"""
    s = normalize_text(name)
    s = re.sub(r'\([^)]*\)', '', s)
    s = re.sub(r'[^0-9a-z\u4e00-\u9fff]', '', s)
    return s


def best_match(refname, pred_names):
    """在预测名称里找最相似的一条，返回 (相似度, 预测名)。"""
    a = simplify(refname)
    if not a:
        return 0.0, ''
    best = (0.0, '')
    for pn in pred_names:
        b = simplify(pn)
        if not b:
            continue
        r = difflib.SequenceMatcher(None, a, b).ratio()
        # 包含关系（'白细胞' 是 '白细胞计数' 的子串）也算高度相似
        if a in b or b in a:
            r = max(r, 0.9)
        if r > best[0]:
            best = (r, pn)
    return best


def main():
    args = parse_args()

    gold = {}
    for f in glob.glob(os.path.join(args.gold, '*.json')):
        if os.path.basename(f) in ('manifest.json', 'upload_manifest.json'):
            continue
        d = json.load(open(f, encoding='utf-8'))
        gold[d['report_id']] = d

    preds = {}
    for line in open(args.predictions, encoding='utf-8'):
        if line.strip():
            o = json.loads(line)
            preds[o['image']] = o.get('items') or []

    stat = collections.Counter()
    cases = {'alias': [], 'partial_read': [], 'struct': []}
    cond_tot = collections.Counter()
    cond_ok = collections.Counter()

    for f in sorted(glob.glob(os.path.join(args.artifacts, '*.json'))):
        if os.path.basename(f).startswith('_'):
            continue
        rec = json.load(open(f, encoding='utf-8'))
        img = rec.get('image', '')
        rid = os.path.splitext(img)[0]
        g = gold.get(rid)
        if not g or img not in preds:
            continue

        ocr = normalize_text(ocr_fulltext(rec))
        ref_items = g['items']
        pred_items = preds[img]
        pred_names = [p.get('item_name') or '' for p in pred_items]

        pairs = ev7.match_items(pred_items, ref_items, loose=args.loose)
        matched_ref = {id(r) for _, r in pairs}
        stat['all_hit'] += len(pairs)

        # 条件准确率：只在"名称命中"的项目上，看值/单位/区间抽对的比例
        for pred, ref in pairs:
            r = ev7.gold_item_to_pred_shape(ref)
            for fld in ('item_value', 'item_unit', 'item_range'):
                cond_tot[fld] += 1
                if ev7.field_equal(fld, pred.get(fld), r.get(fld)):
                    cond_ok[fld] += 1

        for it in ref_items:
            stat['gt'] += 1
            vis = visibility(ref_name(it), ocr)
            stat['vis_' + vis] += 1
            if vis != 'full':
                continue
            stat['full'] += 1
            if id(it) in matched_ref:
                stat['full_hit'] += 1
                continue
            # 未命中的 full 项目：归类
            ratio, pname = best_match(ref_name(it), pred_names)
            if ratio >= args.near:
                stat['miss_alias'] += 1
                if len(cases['alias']) < 12:
                    cases['alias'].append((img, ref_name(it), pname, round(ratio, 2)))
            elif ratio >= 0.4:
                stat['miss_partial_read'] += 1
                if len(cases['partial_read']) < 12:
                    cases['partial_read'].append((img, ref_name(it), pname, round(ratio, 2)))
            else:
                stat['miss_struct'] += 1
                if len(cases['struct']) < 12:
                    cases['struct'].append((img, ref_name(it), pname, round(ratio, 2)))

    full = stat['full'] or 1
    gt = stat['gt'] or 1
    miss = stat['full'] - stat['full_hit']

    print('=' * 72)
    print('item_name 未命中原因拆解（分母 = 完全可见的项目）')
    print('=' * 72)
    print(f'金标准项目        {stat["gt"]}')
    print(f'  完全可见(full)  {stat["full"]}  ({stat["full"]/gt*100:.2f}%)')
    print(f'  部分/不可见     {stat["gt"]-stat["full"]}  '
          f'({(stat["gt"]-stat["full"])/gt*100:.2f}%)  ← 识别层无法弥补')
    print()
    print(f'全量口径上限（全部 full 都命中）= {stat["full"]/gt*100:.2f}%')
    print(f'当前全量口径（含 partial 也命中的）= '
          f'{stat["all_hit"]/gt*100:.2f}%  ({stat["all_hit"]}/{stat["gt"]})')
    print(f'其中来自完全可见项目的           = {stat["full_hit"]/gt*100:.2f}%'
          f'  ({stat["full_hit"]}/{stat["gt"]})')
    print(f'可见口径（分母=仅 full）          = {stat["full_hit"]/full*100:.2f}%'
          f'  ({stat["full_hit"]}/{stat["full"]})')
    print()
    print(f'可见项目里没命中的 {miss} 项，原因分布：')
    for key, label in (('miss_alias', '名称写法差异（加别名归一化可救）'),
                       ('miss_partial_read', '读到了但残缺/串行（需更强识别或结构约束）'),
                       ('miss_struct', '完全没抽出来（表格结构/跨页问题）')):
        c = stat[key]
        print(f'  {label:<32} {c:>3}  {c/miss*100 if miss else 0:>6.2f}%')
    print()
    print('=' * 72)
    print('条件准确率（名称命中之后，各字段抽对的比例）')
    print('=' * 72)
    for fld in ('item_value', 'item_unit', 'item_range'):
        t = cond_tot[fld] or 1
        print(f'  {fld:<12} {cond_ok[fld]/t*100:>6.2f}%   ({cond_ok[fld]}/{t})')
    print('  说明：这三个数字高，说明"读到了就基本读对"，问题不在字段抽取。')
    print()

    for key, title in (('alias', '名称写法差异样例'),
                       ('partial_read', '残缺/串行样例'),
                       ('struct', '完全没抽出示例')):
        if not cases[key]:
            continue
        print('-' * 72)
        print(title)
        print('-' * 72)
        for img, refn, predn, r in cases[key]:
            print(f'  [{r}] 金标={refn!r}')
            print(f'        预测={predn!r}   ({img})')
    print()

    with open(args.out, 'w', encoding='utf-8') as fo:
        fo.write('# item_name 未命中原因拆解\n\n')
        fo.write(f'- 金标准项目 {stat["gt"]}，完全可见 {stat["full"]} '
                 f'({stat["full"]/gt*100:.2f}%)\n')
        fo.write(f'- 全量口径上限（全部可见都命中）{stat["full"]/gt*100:.2f}%\n')
        fo.write(f'- 当前全量口径 {stat["full_hit"]/gt*100:.2f}% '
                 f'({stat["full_hit"]}/{stat["gt"]})\n')
        fo.write(f'- 当前可见口径 {stat["full_hit"]/full*100:.2f}% '
                 f'({stat["full_hit"]}/{stat["full"]})\n\n')
        fo.write('## 可见项目未命中的原因分布\n\n')
        fo.write('| 原因 | 数量 | 占比 |\n| --- | --- | --- |\n')
        for key, label in (('miss_alias', '名称写法差异（别名归一化可救）'),
                           ('miss_partial_read', '读到了但残缺/串行'),
                           ('miss_struct', '完全没抽出来（结构问题）')):
            c = stat[key]
            fo.write(f'| {label} | {c} | {c/miss*100 if miss else 0:.2f}% |\n')
        fo.write('\n## 条件准确率（名称命中后）\n\n')
        fo.write('| 字段 | 准确率 | 命中/总数 |\n| --- | --- | --- |\n')
        for fld in ('item_value', 'item_unit', 'item_range'):
            t = cond_tot[fld] or 1
            fo.write(f'| {fld} | {cond_ok[fld]/t*100:.2f}% | '
                     f'{cond_ok[fld]}/{t} |\n')
        for key, title in (('alias', '名称写法差异样例'),
                           ('partial_read', '残缺/串行样例'),
                           ('struct', '完全没抽出示例')):
            if not cases[key]:
                continue
            fo.write(f'\n## {title}\n\n| 相似度 | 金标准 | 预测 | 样本 |\n'
                     '| --- | --- | --- | --- |\n')
            for img, refn, predn, r in cases[key]:
                fo.write(f'| {r} | `{refn}` | `{predn}` | {img} |\n')
    print(f'报告: {args.out}')


if __name__ == '__main__':
    main()
