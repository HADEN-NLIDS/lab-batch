# -*- coding: utf-8 -*-
r"""21 本地：区分"完全可见/部分打码/完全不可见"，并给出两种口径的 recall

为什么需要：
    tools/20_diag_censored.py 用字符重合率≥0.8 判可见，把"部分打码"也当成可见，
    得到 91.86% 的可见率。但人工核对发现，缺失项多为名字被涂掉几个字
    （如"凝血酶原时间"只剩"准化时间"），OCR 只能读到残缺名，评测必然不匹配。

用法：
    python tools/21_visible_recall.py --artifacts ..\..\artifacts ^
        --gold ..\..\eval_gold_medrep --predictions predictions.jsonl

历史工具：为公开数据集搭评测闭环时编写。该数据集脱敏较重、版面结构受损，
识别效果不具代表性，当前测试以真实报告单脱敏为主，此脚本保留作参考。
"""

import os
import re
import sys
import json
import glob
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

SEP = '#################'


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', required=True)
    ap.add_argument('--gold', required=True)
    ap.add_argument('--predictions', required=True)
    ap.add_argument('--out', default='visible_recall.md')
    return ap.parse_args()


# ---------- 与 07_evaluate.py 保持一致的归一化 ----------
def normalize_text(v):
    v = '' if v is None else str(v)
    v = v.strip().lower().replace(' ', '')
    v = v.replace('：', ':').replace('－', '-').replace('—', '-').replace('–', '-')
    v = v.replace('~', '-').replace('～', '-')
    return v


def normalize_number(v):
    v = normalize_text(v)
    return re.sub(r'(?<=\d)\.0+(?=\D|$)', '', v)


def field_equal(field, pred, ref):
    if field in ('item_value', 'item_range'):
        return normalize_number(pred) == normalize_number(ref)
    return normalize_text(pred) == normalize_text(ref)


def ocr_fulltext(rec):
    parts = []
    for res in (rec.get('results') or []):
        for s in (res.get('parsing_res_list') or []):
            if not isinstance(s, str):
                continue
            for part in s.split(SEP):
                m = re.search(r'content:\s*(.*)', part, re.S)
                if not m:
                    continue
                c = re.sub(r'<[^>]+>', ' ', m.group(1))
                c = (c.replace('&nbsp;', ' ').replace('&lt;', '<')
                      .replace('&gt;', '>').replace('&amp;', '&'))
                parts.append(c)
    text = ' '.join(parts)
    # 单元格内的换行被 OCR 输出成字面量 '\n'（反斜杠+n），不去掉会让人误判成"名字被打码"
    text = text.replace('\\r\\n', ' ').replace('\\n', ' ').replace('\\t', ' ')
    return text


def visibility(name, ocr_norm):
    """三档判定：full / partial / none"""
    n = normalize_text(name)
    if not n:
        return 'none'
    if n in ocr_norm:
        return 'full'
    # 去掉括号内容再试
    n2 = re.sub(r'\([^)]*\)', '', n)
    if n2 and n2 in ocr_norm:
        return 'full'
    # 部分：字符覆盖率 0.5~1.0
    if len(n2) >= 3:
        hit = sum(1 for ch in n2 if ch in ocr_norm)
        ratio = hit / len(n2)
        if ratio >= 0.8:
            return 'partial'      # 名字大部分字在，但整体串不匹配 → 疑似部分打码
        if ratio >= 0.5:
            return 'partial'
    return 'none'


def ref_name(ref):
    """gold 里字段名是 name；兼容 item_name（CSV 来源用后者）。"""
    return ref.get('item_name') or ref.get('name') or ''


def match_items(pred_items, ref_items):
    """官方口径：按 item_name 匹配，每条 ref 至多配一条 pred"""
    used = set()
    pairs = []
    for ref in ref_items:
        rn = normalize_text(ref_name(ref))
        for i, p in enumerate(pred_items):
            if i in used:
                continue
            if normalize_text(p.get('item_name')) == rn:
                used.add(i)
                pairs.append((p, ref))
                break
    return pairs


FIELDS = ['item_name', 'item_value', 'item_unit', 'item_range']


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

    # 统计
    vis_counter = collections.Counter()
    full_tot = collections.Counter()
    full_ok = collections.Counter()
    all_tot = collections.Counter()
    all_ok = collections.Counter()
    per_sample = []

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
        ref_items = g.get('items', [])
        pred_items = preds[img]

        # 标注每个 ref 的可见性
        vis = [visibility(ref_name(it), ocr) for it in ref_items]
        for v in vis:
            vis_counter[v] += 1

        pairs = match_items(pred_items, ref_items)

        # 全量口径（只算 item_name：gold 与 predictions 的字段名不同，
        # 其他字段的口径以 07_evaluate.py 对 CSV 的评测为准）
        all_tot['item_name'] += len(ref_items)
        all_ok['item_name'] += len(pairs)

        # 可见口径：分母只算 full 的项目
        full_ids = {id(it) for it, v in zip(ref_items, vis) if v == 'full'}
        full_tot['item_name'] += len(full_ids)
        full_ok['item_name'] += sum(1 for _, r in pairs if id(r) in full_ids)

        per_sample.append({
            'image': img, 'gt': len(ref_items),
            'full': sum(1 for v in vis if v == 'full'),
            'partial': sum(1 for v in vis if v == 'partial'),
            'none': sum(1 for v in vis if v == 'none'),
            'pred': len(pred_items),
        })

    n = sum(vis_counter.values())
    print('=' * 70)
    print('可见性三档判定')
    print('=' * 70)
    print(f'样本 {len(per_sample)}  项目 {n}')
    for k in ('full', 'partial', 'none'):
        c = vis_counter[k]
        print(f'  {k:<8} {c:>4}  {c/n*100:>6.2f}%')

    print()
    print('=' * 70)
    print('item_name 双口径 recall')
    print('=' * 70)
    a = all_ok['item_name'] / all_tot['item_name'] * 100 if all_tot['item_name'] else 0
    b = full_ok['item_name'] / full_tot['item_name'] * 100 if full_tot['item_name'] else 0
    print(f'  全量口径（分母=全部项目）  {a:>6.2f}%   '
          f'命中{all_ok["item_name"]}/{all_tot["item_name"]}')
    print(f'  可见口径（分母=仅full）    {b:>6.2f}%   '
          f'命中{full_ok["item_name"]}/{full_tot["item_name"]}')
    print(f'  差值                      {b-a:>+6.2f} 个百分点')
    print()
    print('  解读：')
    if b - a > 10:
        print(f'    两者差距明显，说明低 recall 中有相当部分是"项目名被打码"造成的，')
        print(f'    不是解析器能力不足。论文应同时报告这两个口径。')
    else:
        print('    差距不大，说明低 recall 主要是解析/抽取问题，数据打码影响有限。')

    print()
    print('=' * 70)
    print('逐样本可见性')
    print('=' * 70)
    print(f'{"image":<34}{"GT":>4}{"full":>6}{"partial":>9}{"none":>6}')
    print('-' * 70)
    for r in sorted(per_sample, key=lambda x: x['image']):
        print(f"{r['image'][:32]:<34}{r['gt']:>4}{r['full']:>6}"
              f"{r['partial']:>9}{r['none']:>6}")

    with open(args.out, 'w', encoding='utf-8') as fo:
        fo.write('# 可见性与双口径 recall\n\n')
        fo.write(f'- 样本 {len(per_sample)}，项目 {n}\n')
        for k in ('full', 'partial', 'none'):
            c = vis_counter[k]
            fo.write(f'- {k}: {c} ({c/n*100:.2f}%)\n')
        fo.write('\n## item_name 双口径\n\n')
        fo.write('| 口径 | recall | 命中/总数 |\n| --- | --- | --- |\n')
        fo.write(f'| 全量（分母=全部项目） | {a:.2f}% | '
                 f'{all_ok["item_name"]}/{all_tot["item_name"]} |\n')
        fo.write(f'| 可见（分母=仅full） | {b:.2f}% | '
                 f'{full_ok["item_name"]}/{full_tot["item_name"]} |\n')
        fo.write('\n## 逐样本\n\n| image | GT | full | partial | none |\n')
        fo.write('| --- | --- | --- | --- | --- |\n')
        for r in sorted(per_sample, key=lambda x: x['image']):
            fo.write(f"| {r['image']} | {r['gt']} | {r['full']} | "
                     f"{r['partial']} | {r['none']} |\n")
    print(f'\n报告: {args.out}')


if __name__ == '__main__':
    main()
