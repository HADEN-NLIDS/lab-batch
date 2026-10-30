# -*- coding: utf-8 -*-
r"""39 本地：对比不同像素档位的识别结果

配合 38_make_scale_variants.py 使用。读的是 03 在变体图上跑出的 artifacts。

用法
----
    python tools\39_compare_scales.py --artifacts artifacts_scale ^
        --gold "<数据目录>\自采数据集\gold" --report scale_report.md"""

import os
import re
import sys
import json
import glob
import argparse
import importlib.util

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))


def load(fname, modname):
    spec = importlib.util.spec_from_file_location(modname,
                                                 os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', required=True)
    ap.add_argument('--gold', default='', help='自采数据集的 gold 目录（可选）')
    ap.add_argument('--report', default='scale_report.md')
    return ap.parse_args()


def norm_stem(s):
    """去掉 _masked 等后缀，用于和 gold 的 report_id 对齐。

    artifacts 的文件名常是 'xxx_masked__x2'，拆出来的 stem 是 'xxx_masked'，
    而 gold 的 report_id 是 'xxx'，不归一就查不到标准答案。
    """
    return re.sub(r'(_masked|_annot|_已脱敏|_脱敏)$', '', s or '', flags=re.I)


def load_gold(path):
    if not path or not os.path.isdir(path):
        return {}
    out = {}
    for f in glob.glob(os.path.join(path, '*.json')):
        if os.path.basename(f) == 'manifest.json':
            continue
        d = json.load(open(f, encoding='utf-8'))
        out[d['report_id']] = d['items']
    return out


def main():
    args = parse_args()
    p4 = load('04_parse_predictions.py', 'p04_for39')
    ev7 = load('07_evaluate.py', 'ev7_for39')

    gold = load_gold(args.gold)
    files = sorted(glob.glob(os.path.join(args.artifacts, '*.json')))
    files = [f for f in files if not os.path.basename(f).startswith('_')]
    if not files:
        print('没有 artifacts:', args.artifacts)
        return 1

    # 文件名格式：<原图名>__x<档位>
    PAT = re.compile(r'^(?P<stem>.+?)__x(?P<scale>[\d_]+)$')

    rows = []
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        m = PAT.match(base)
        if not m:
            continue
        stem = m.group('stem')
        scale = m.group('scale').replace('_', '.')
        try:
            scale = float(scale)
        except ValueError:
            continue
        rec = json.load(open(f, encoding='utf-8'))

        items = []
        table_html = p4.extract_tables(p4.get_html_sources(rec))
        for tb in table_html:
            for row in tb:
                items.extend(p4.extract_row_items(row))

        n = len(items)
        n_unit = sum(1 for x in items if (x.get('item_unit') or '').strip())
        n_range = sum(1 for x in items if (x.get('item_range') or '').strip())

        # 与 gold 比对：把 gold item 转成配对器认得的形状，复用 07 的 match_items
        gm = []
        gkey = norm_stem(stem)
        if gkey in gold:
            refs = [{'item_name': g.get('name'), 'name': g.get('name'),
                     'value': g.get('value'), 'unit': g.get('unit'),
                     'reference': g.get('reference'),
                     'flag': g.get('flag')} for g in gold[gkey]]
            gm = ev7.match_items(items, refs, loose=True)
        n_gold = len(gold.get(norm_stem(stem)) or [])
        name_hit = len(gm)
        unit_hit = 0
        value_hit = 0
        range_hit = 0
        for pred, ref in gm:
            r = ev7.gold_item_to_pred_shape(ref)
            if ev7.field_equal('item_unit', pred.get('item_unit'), r.get('item_unit')):
                unit_hit += 1
            if ev7.field_equal('item_value', pred.get('item_value'), r.get('item_value')):
                value_hit += 1
            if ev7.field_equal('item_range', pred.get('item_range'), r.get('item_range')):
                range_hit += 1

        rows.append({
            'stem': stem, 'scale': scale, 'items': n,
            'unit': n_unit, 'range': n_range,
            'gold': n_gold, 'name_hit': name_hit,
            'unit_hit': unit_hit, 'value_hit': value_hit,
            'range_hit': range_hit,
            'size': (rec.get('image_size') or ''),
        })

    if not rows:
        print('文件名不符合 <原图名>__x<档位> 格式，无法分组。')
        print('示例：', [os.path.basename(f) for f in files[:3]])
        return 1

    # 按图分组、档位排序
    by_stem = {}
    for r in rows:
        by_stem.setdefault(r['stem'], []).append(r)
    for k in by_stem:
        by_stem[k].sort(key=lambda x: x['scale'])

    print('=' * 84)
    print('不同像素档位的识别效果')
    print('=' * 84)
    for stem, rs in by_stem.items():
        print()
        print('### %s' % stem)
        header = ('  %-8s %6s %6s %6s' % ('档位', '项目数', '有单位', '有区间'))
        if rs[0]['gold']:
            header += ' %8s %8s %8s %8s' % ('名称命中', '值命中', '单位命中', '区间命中')
        print(header)
        print('  ' + '-' * (len(header) - 2))
        for r in rs:
            line = ('  %-8s %6d %6d %6d'
                    % ('%gx' % r['scale'], r['items'], r['unit'], r['range']))
            if r['gold']:
                line += (' %8s %8s %8s %8s'
                         % ('%d/%d' % (r['name_hit'], r['gold']),
                            '%d/%d' % (r['value_hit'], r['gold']),
                            '%d/%d' % (r['unit_hit'], r['gold']),
                            '%d/%d' % (r['range_hit'], r['gold'])))
            print(line)

        # 判定是否"模型内部缩小了"
        sigs = {}
        for r in rs:
            sig = (r['items'], r['unit'], r['range'])
            sigs.setdefault(sig, []).append(r['scale'])
        if len(sigs) < len(rs):
            dup = [v for v in sigs.values() if len(v) > 1]
            print()
            print('  注意：这些档位的结果**完全相同** → %s'
                  % ' / '.join('%gx' % s for s in sorted(dup[0])))
            print('        说明图在进入模型前被 resize 到了同一尺寸，'
                  '继续放大不会带来新信息。')

    # 汇总：单位列是否在某档位才出现
    print()
    print('=' * 84)
    print('单位列的可读出档位')
    print('=' * 84)
    for stem, rs in by_stem.items():
        ok = [r['scale'] for r in rs if r['unit'] > 0]
        if ok:
            print('  %-30s 在 %s 档位读出单位（最少 %gx）'
                  % (stem, ' / '.join('%gx' % s for s in ok), min(ok)))
        else:
            print('  %-30s 所有档位都没读出单位 → 不是分辨率问题' % stem)

    with open(args.report, 'w', encoding='utf-8') as fp:
        fp.write('# 不同像素档位的识别效果\n\n')
        for stem, rs in by_stem.items():
            fp.write('## %s\n\n' % stem)
            fp.write('| 档位 | 项目数 | 有单位 | 有区间 | 名称命中 | 值命中 | 单位命中 | 区间命中 |\n')
            fp.write('| --- | --- | --- | --- | --- | --- | --- | --- |\n')
            for r in rs:
                fp.write('| %gx | %d | %d | %d | %d/%d | %d/%d | %d/%d | %d/%d |\n'
                         % (r['scale'], r['items'], r['unit'], r['range'],
                            r['name_hit'], r['gold'], r['value_hit'], r['gold'],
                            r['unit_hit'], r['gold'], r['range_hit'], r['gold']))
            fp.write('\n')
    print()
    print('报告:', args.report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
