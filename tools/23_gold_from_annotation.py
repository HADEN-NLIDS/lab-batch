# -*- coding: utf-8 -*-
r"""23 本地：把标注结果转成 eval_gold 格式 + 收拢脱敏图

输入（由 annotator.html 导出）：
    <out_dir>/output/*_annot.json     标注
    <out_dir>/output/*_masked.png     脱敏图

用法：
    python tools\23_gold_from_annotation.py"""

import os
import re
import sys
import json
import glob
import shutil
import argparse

sys.stdout.reconfigure(encoding='utf-8')

DASHES = ['－', '—', '–', '~', '～', '〜']
NUM = r'[-+]?\d+(?:\.\d+)?'


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--src', default=r'<数据目录>\标注工具\output')
    ap.add_argument('--out', default=r'<数据目录>\自采数据集')
    ap.add_argument('--prune-stale', action='store_true',
                    help='生成后删掉 gold 目录里不属于本次的旧 json。'
                         '旧文件的 report_id 可能带 _masked 后缀，与新文件共存；'
                         '评测时归一化会让两组撞键，旧文件（flag 多为 unknown）'
                         '覆盖新文件，表现为整批评测准确率 0%。')
    return ap.parse_args()


def norm_dash(s):
    for d in DASHES:
        s = s.replace(d, '-')
    return s


def parse_reference(raw):
    """与 05_adapt_medrepbench.py 保持一致的解析逻辑。"""
    s = '' if raw is None else str(raw).strip()
    if not s:
        return {'kind': 'missing', 'raw': ''}
    if re.fullmatch(r'[-－—–~～\s./|、]+', s):
        return {'kind': 'missing', 'raw': s}
    s2 = norm_dash(s).replace(' ', '')
    s2 = s2.replace('＜', '<').replace('＞', '>').replace('≤', '<').replace('≥', '>')

    m = re.fullmatch(r'[<]\s*(' + NUM + r')', s2)
    if m:
        return {'kind': 'one_sided', 'high': m.group(1), 'raw': s}
    m = re.fullmatch(r'[>]\s*(' + NUM + r')', s2)
    if m:
        return {'kind': 'one_sided', 'low': m.group(1), 'raw': s}
    # 双横线优先（MedRepBench 用 -- 表示区间）
    m = re.fullmatch(r'(' + NUM + r')--(' + NUM + r')', s2)
    if m:
        return {'kind': 'interval', 'low': m.group(1), 'high': m.group(2), 'raw': s}
    m = re.fullmatch(r'(' + NUM + r')-(' + NUM + r')', s2)
    if m:
        return {'kind': 'interval', 'low': m.group(1), 'high': m.group(2), 'raw': s}
    return {'kind': 'text', 'text': s, 'raw': s}


def parse_value(raw):
    s = '' if raw is None else str(raw).strip()
    if not s:
        return '', 'missing'
    for sym, op in (('<', 'lt'), ('>', 'gt'), ('＜', 'lt'), ('＞', 'gt')):
        if s.startswith(sym):
            return s[1:].strip(), op
    return s, 'eq'


def load_samples(src):
    """读标注来源，统一成 [sample dict]。

    支持两种格式：
      · 旧：每张一个 *_annot.json（22/25 生成、逐张下载）
      · 新：一个 review_export_*.json 打包全部（32 生成）
    `--src` 既可以是目录，也可以直接指向某个 JSON 文件。
    """
    if os.path.isfile(src):
        files = [src]
    else:
        files = (sorted(glob.glob(os.path.join(src, '*_annot.json')))
                 + sorted(glob.glob(os.path.join(src, 'review_export_*.json'))))
    if not files:
        return []

    samples = {}
    for f in files:
        try:
            d = json.load(open(f, encoding='utf-8'))
        except (ValueError, OSError) as e:
            print(f'  跳过 {os.path.basename(f)}：读不出来（{e}）')
            continue
        if isinstance(d, dict) and isinstance(d.get('samples'), list):
            for s in d['samples']:
                rid = s.get('report_id') or ''
                if rid:
                    samples[rid] = s
        else:
            rid = (d.get('report_id')
                   or os.path.splitext(os.path.basename(f))[0]).replace('_annot', '')
            d['report_id'] = rid
            samples[rid] = d
    return [samples[k] for k in sorted(samples)]


def norm_flag(raw):
    """人工标注的 flag。空/非法一律当 unknown——不臆造。

    'unknown' 表示"人工判不了"（没参考区间、结果缺失等），
    与 is_abnormal 留空的语义一致，评测时不计入分母。
    """
    v = ('' if raw is None else str(raw)).strip().lower()
    if v in ('normal', 'abnormal'):
        return v
    return 'unknown'


def main():
    args = parse_args()
    samples = load_samples(args.src)
    if not samples:
        print(f'{args.src} 下没有标注文件')
        print('  支持 *_annot.json（旧，逐张导出）和 review_export_*.json（新，一次导出全部）')
        print('  请先用 32_make_review_tool.py 生成复核工具并标注导出')
        sys.exit(1)

    img_out = os.path.join(args.out, 'images')
    gold_out = os.path.join(args.out, 'gold')
    os.makedirs(img_out, exist_ok=True)
    os.makedirs(gold_out, exist_ok=True)

    manifest = []
    total_items = 0
    flag_stat = {'normal': 0, 'abnormal': 0, 'unknown': 0}
    prefilled = 0
    overturned = 0

    for s in samples:
        rid = s.get('report_id') or ''

        raw_items = [it for it in (s.get('items') or [])
                     if (it.get('name') or '').strip()]
        if not raw_items:
            print(f'  跳过 {rid}：无标注项目')
            continue

        gold_items = []
        for it in raw_items:
            val, op = parse_value(it.get('value'))
            ref = parse_reference(it.get('range_raw'))
            flag = norm_flag(it.get('flag'))
            item = {
                'name': it['name'].strip(),
                'indicator_code': None,
                'value': val,
                'value_operator': op,
                'unit': (it.get('unit') or '').strip(),
                'reference': ref,
                'flag': flag,
                'triage': None,
            }
            # 追溯用：规则引擎当时的预填值。能算出"人工推翻了机器多少次"
            rj = it.get('rule_judged')
            if rj in ('normal', 'abnormal'):
                prefilled += 1
                item['rule_prefill'] = {'flag': rj,
                                        'conf': it.get('rule_conf') or ''}
                if flag in ('normal', 'abnormal') and flag != rj:
                    overturned += 1
            gold_items.append(item)
            flag_stat[flag] += 1

        doc = {
            'report_id': rid,
            'revision': 1,
            'expected_triage': None,
            'expected_completeness': 'full',
            'source': 'self-collected (de-identified)',
            'note': ('自采数据人工标注。flag 为人工判定结果，unknown 表示人工也判不了'
                     '（无参考区间/结果缺失），评测时不计入分母。'),
            'items': gold_items,
        }
        with open(os.path.join(gold_out, rid + '.json'), 'w', encoding='utf-8') as fp:
            json.dump(doc, fp, ensure_ascii=False, indent=2)

        # 收拢脱敏图
        masked = s.get('masked_image') or (rid + '_masked.png')
        src_img = os.path.join(os.path.dirname(args.src.rstrip('/\\')), masked)
        if not os.path.isfile(src_img):
            src_img = os.path.join(args.src, masked)
        if os.path.isfile(src_img):
            shutil.copy2(src_img, os.path.join(img_out, rid + '.png'))
        else:
            print(f'  !! {rid} 的脱敏图未找到: {masked}')

        manifest.append({'report_id': rid, 'image': rid + '.png',
                         'num_items': len(gold_items), 'meta_type': 'Laboratory'})
        total_items += len(gold_items)
        print(f'  {rid}: {len(gold_items)} 项')

    with open(os.path.join(gold_out, 'manifest.json'), 'w', encoding='utf-8') as fp:
        json.dump({'count': len(manifest), 'samples': manifest}, fp,
                  ensure_ascii=False, indent=2)

    if args.prune_stale:
        keep = {s['report_id'] + '.json' for s in manifest} | {'manifest.json'}
        removed = []
        for f in glob.glob(os.path.join(gold_out, '*.json')):
            if os.path.basename(f) not in keep:
                os.remove(f)
                removed.append(os.path.basename(f))
        if removed:
            print()
            print('清理了 %d 个不属于本批次的旧 gold 文件：' % len(removed))
            for n in removed:
                print('  -', n)

    print()
    print('=' * 56)
    print('转换完成')
    print('=' * 56)
    print(f'样本 {len(manifest)} 份，项目 {total_items} 个')
    print('  flag 分布: normal %d / abnormal %d / unknown %d'
          % (flag_stat['normal'], flag_stat['abnormal'], flag_stat['unknown']))
    if prefilled:
        rate = overturned / prefilled * 100
        print('  规则引擎预填过的 %d 项中，人工推翻了 %d 项（%.1f%%）'
              % (prefilled, overturned, rate))
        print('    ↑ 这个数就是规则引擎在真实数据上的"错判率"，越小越好')
    if flag_stat['unknown'] == total_items and total_items:
        print()
        print('  !! 全部是 unknown，说明标注时没人动过"异常"那一栏。')
        print('     用 32_make_review_tool.py 生成的新版工具才有这一列。')
    print(f'  图片: {img_out}')
    print(f'  gold: {gold_out}')
    print()
    print('后续可直接跑：')
    print(f'  python tools\\03_export_ocr.py --gold-dir "{gold_out}" '
          f'--src "{args.out}" --out-dir artifacts_self')
    print('  python tools\\04_parse_predictions.py --artifacts artifacts_self '
          f'--gold-dir "{gold_out}" --out predictions_self.jsonl')
    print('  python tools\\07_evaluate.py --gold-dir '
          f'"{gold_out}" --predictions predictions_self.jsonl')


if __name__ == '__main__':
    main()
