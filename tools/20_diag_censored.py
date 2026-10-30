# -*- coding: utf-8 -*-
r"""20 本地：量化"图片打码"对评测的影响

背景：
    用户目视检查发现 MedRepBench 的图片中，除个人信息外，
    检查项目名称与数值也被打码，导致无法识别或识别不全。

用法：
    python tools/20_diag_censored.py --artifacts ..\..\artifacts --gold ..\..\eval_gold_medrep

历史工具：为公开数据集搭评测闭环时编写。该数据集脱敏较重、版面结构受损，
识别效果不具代表性，当前测试以真实报告单脱敏为主，此脚本保留作参考。
"""

import os
import re
import sys
import json
import glob
import argparse

sys.stdout.reconfigure(encoding='utf-8')

SEP = '#################'


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', required=True)
    ap.add_argument('--gold', required=True)
    ap.add_argument('--out', default='censor_diag.md')
    return ap.parse_args()


def ocr_fulltext(rec):
    """把 artifacts 里所有 block 的 content 拼成一大段文本（含HTML标签剥离）。"""
    parts = []
    for res in (rec.get('results') or []):
        for s in (res.get('parsing_res_list') or []):
            if not isinstance(s, str):
                continue
            for part in s.split(SEP):
                m = re.search(r'content:\s*(.*)', part, re.S)
                if not m:
                    continue
                c = m.group(1)
                c = re.sub(r'<[^>]+>', ' ', c)
                c = (c.replace('&nbsp;', ' ').replace('&lt;', '<')
                      .replace('&gt;', '>').replace('&amp;', '&'))
                parts.append(c)
    return ' '.join(parts)


def norm(s):
    """归一化：去空白、小写、全角转半角常用符号。"""
    s = (s or '').lower()
    s = re.sub(r'\s+', '', s)
    s = s.replace('（', '(').replace('）', ')')
    s = s.replace('：', ':').replace('－', '-').replace('—', '-')
    return s


def name_visible(name, ocr_norm):
    """判断项目名是否在 OCR 文本中出现。"""
    n = norm(name)
    if not n:
        return False
    if n in ocr_norm:
        return True
    # 容忍部分缺失：去掉括号内容后再试
    n2 = re.sub(r'\([^)]*\)', '', n)
    if n2 and n2 in ocr_norm:
        return True
    # 长名称：字符重合率≥0.8 视为可见（应对异名/断字）
    if len(n2) >= 4:
        hit = sum(1 for ch in n2 if ch in ocr_norm)
        if hit / len(n2) >= 0.8:
            return True
    return False


def main():
    args = parse_args()

    gold_map = {}
    for f in glob.glob(os.path.join(args.gold, '*.json')):
        if os.path.basename(f) in ('manifest.json', 'upload_manifest.json'):
            continue
        d = json.load(open(f, encoding='utf-8'))
        gold_map[d['report_id']] = d

    rows = []
    total_names = 0
    visible_names = 0

    for f in sorted(glob.glob(os.path.join(args.artifacts, '*.json'))):
        if os.path.basename(f).startswith('_'):
            continue
        rec = json.load(open(f, encoding='utf-8'))
        img = rec.get('image', '')
        rid = os.path.splitext(img)[0]
        g = gold_map.get(rid)
        if not g:
            continue

        ocr = norm(ocr_fulltext(rec))
        names = [it.get('name', '') for it in g.get('items', [])]
        vis = [n for n in names if name_visible(n, ocr)]

        total_names += len(names)
        visible_names += len(vis)
        rows.append({
            'image': img,
            'gt_items': len(names),
            'visible': len(vis),
            'ratio': len(vis) / len(names) if names else 0,
            'missing': [n for n in names if n not in vis][:6],
            'ocr_len': len(ocr),
        })

    if not rows:
        print('没有可对比的样本（检查 artifacts 与 gold 是否对应）')
        sys.exit(1)

    print('=' * 70)
    print('打码影响诊断：gold项目名 在 OCR 文本中的可见率')
    print('=' * 70)
    print(f'样本数: {len(rows)}')
    print(f'项目名总数: {total_names}')
    print(f'可见: {visible_names}  ({visible_names/total_names*100:.2f}%)')
    print(f'不可见: {total_names - visible_names}  '
          f'({(total_names-visible_names)/total_names*100:.2f}%)')
    print()
    print(f'{"image":<36} {"GT":>4} {"可见":>4} {"比率":>7} {"OCR字符数":>9}')
    print('-' * 70)
    for r in sorted(rows, key=lambda x: x['ratio']):
        print(f"{r['image'][:34]:<36} {r['gt_items']:>4} {r['visible']:>4} "
              f"{r['ratio']*100:>6.1f}% {r['ocr_len']:>9}")

    # 最差的几个样本，列出缺失的项目名
    print()
    print('=' * 70)
    print('可见率最低的样本，缺失的项目名')
    print('=' * 70)
    for r in sorted(rows, key=lambda x: x['ratio'])[:5]:
        print(f"\n{r['image']}  GT={r['gt_items']} 可见={r['visible']}")
        for n in r['missing']:
            print(f'    ✗ {n}')

    # 写报告
    with open(args.out, 'w', encoding='utf-8') as fo:
        fo.write('# 打码影响诊断\n\n')
        fo.write(f'- 样本数：{len(rows)}\n')
        fo.write(f'- 项目名总数：{total_names}\n')
        fo.write(f'- **可见 {visible_names} ({visible_names/total_names*100:.2f}%)**\n')
        fo.write(f'- 不可见 {total_names-visible_names} '
                 f'({(total_names-visible_names)/total_names*100:.2f}%)\n\n')
        fo.write('| image | GT项数 | 可见 | 比率 | OCR字符数 |\n')
        fo.write('| --- | --- | --- | --- | --- |\n')
        for r in sorted(rows, key=lambda x: x['ratio']):
            fo.write(f"| {r['image']} | {r['gt_items']} | {r['visible']} | "
                     f"{r['ratio']*100:.1f}% | {r['ocr_len']} |\n")
    print(f'\n报告已写入: {args.out}')

    print()
    print('=' * 70)
    print('判读')
    print('=' * 70)
    rate = visible_names / total_names
    if rate < 0.6:
        print(f'  ⚠ 可见率仅 {rate*100:.1f}%：图片打码严重，')
        print('    规则解析器的 recall 已接近"可见内容"上限，')
        print('    之前"解析器到天花板"的结论需修正为"数据受限"。')
    elif rate < 0.85:
        print(f'  可见率 {rate*100:.1f}%：部分内容不可见，会影响 recall 上限。')
        print('    评测时应剔除可见率过低的样本，或在论文中说明。')
    else:
        print(f'  可见率 {rate*100:.1f}%：大部分内容可识别，')
        print('    recall 低主要是解析/抽取问题，而非数据缺失。')


if __name__ == '__main__':
    main()
