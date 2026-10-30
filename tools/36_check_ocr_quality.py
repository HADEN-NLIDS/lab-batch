# -*- coding: utf-8 -*-
r"""36 本地：OCR 表格质量诊断——找出"表头有这一列、但整列数据都是空"的情况

为什么需要
----------
真实踩到的坑：`144502` 这张图的表格表头有「单位」，但 32 行数据的单位格
全是空的 `<td></td>`。解析器没问题（它把空格子如实读成空），问题是
**OCR 那一次运行没把单位读出来**。而同一张图在别的批次里是读出来的。

用法
----
    python tools\36_check_ocr_quality.py --artifacts .\out
    python tools\36_check_ocr_quality.py --artifacts artifacts --report ocr_quality.md"""

import os
import re
import sys
import json
import glob
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

# 这些列本来就常常是空的，整列为空不算异常
OPTIONAL_HINTS = ('提示', '互认', '标识', '备注', '检测方法', '方法', '状态',
                  '异常提示', '箭头', '英文缩写', '缩写', '序号', 'No.', '代码')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', required=True)
    ap.add_argument('--report', default='ocr_quality.md')
    ap.add_argument('--min-rows', type=int, default=3,
                    help='少于这么多数据行的表格不检查（太小没有统计意义）')
    return ap.parse_args()


def tables_of(rec):
    """取出一份 artifacts 里的所有 HTML 表格。"""
    out = []
    for res in (rec.get('results') or []):
        for s in (res.get('parsing_res_list') or []):
            if not isinstance(s, str):
                continue
            for part in s.split('#################'):
                c = re.search(r'content:\s*(.*)', part, re.S)
                if not c:
                    continue
                for t in re.findall(r'<table[^>]*>.*?</table>', c.group(1), re.S):
                    out.append(t)
    return out


def rows_of(table):
    """把 <table> 拆成 [[cell, ...], ...]，保留 colspan 占位。"""
    rows = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.S):
        cells = []
        for m in re.finditer(r'<t[dh]([^>]*)>(.*?)</t[dh]>', r, re.S):
            attrs, body = m.group(1), m.group(2)
            txt = re.sub(r'<[^>]+>', '', body)
            txt = (txt.replace('&nbsp;', ' ').replace('&lt;', '<')
                      .replace('&gt;', '>').replace('&amp;', '&')).strip()
            span = 1
            mc = re.search(r'colspan\s*=\s*"(\d+)"', attrs)
            if mc:
                span = int(mc.group(1))
            cells.append(txt)
            for _ in range(span - 1):
                cells.append('')      # colspan 补空位，保持列对齐
        if cells:
            rows.append(cells)
    return rows


def main():
    args = parse_args()
    files = sorted(glob.glob(os.path.join(args.artifacts, '*.json')))
    files = [f for f in files if not os.path.basename(f).startswith('_')]

    findings = []
    for f in files:
        try:
            rec = json.load(open(f, encoding='utf-8'))
        except (ValueError, OSError):
            continue
        img = rec.get('image') or os.path.basename(f)
        for ti, table in enumerate(tables_of(rec)):
            rows = rows_of(table)
            if len(rows) < args.min_rows + 1:
                continue
            header = rows[0]
            body = rows[1:]
            ncol = max(len(r) for r in body)
            if ncol < 3:
                continue
            for ci in range(min(len(header), ncol)):
                name = header[ci]
                if not name:
                    continue
                if any(h in name for h in OPTIONAL_HINTS):
                    continue
                filled = sum(1 for r in body
                             if ci < len(r) and (r[ci] or '').strip())
                if filled == 0:
                    findings.append({
                        'image': img, 'table': ti, 'col': ci,
                        'header': name, 'rows': len(body), 'filled': 0,
                    })

    print('=' * 70)
    print('OCR 表格质量诊断')
    print('=' * 70)
    print('  artifacts: %s' % args.artifacts)
    print('  文件 %d 份' % len(files))
    print()

    if not findings:
        print('  没有发现"表头有列但整列为空"的情况。')
    else:
        print('  发现 %d 处可疑："表头有这一列，但整列数据都是空的"' % len(findings))
        print()
        print('  %-46s %-12s %s' % ('文件', '表头列名', '数据行数'))
        print('  ' + '-' * 68)
        by_file = collections.OrderedDict()
        for x in findings:
            by_file.setdefault(x['image'], []).append(x)
        for img, xs in by_file.items():
            for x in xs:
                print('  %-46s %-12s %d'
                      % (os.path.basename(img)[:44], x['header'], x['rows']))
        print()
        print('  这些列需要人工确认原因，三种可能：')
        print('    1. OCR 某次运行漏读了整列（重跑一次通常能恢复）')
        print('    2. 该区域被打码覆盖（MedRepBench 这类样本很常见）')
        print('    3. 报告本身就没印这一列的内容（空列是正常的）')
        print('  判别方法：打开原图看这一列。有内容而 OCR 输出为空 → 属于第 1 种，重跑。')

    with open(args.report, 'w', encoding='utf-8') as fp:
        fp.write('# OCR 表格质量诊断\n\n')
        fp.write(f'- artifacts: `{args.artifacts}`\n')
        fp.write(f'- 文件 {len(files)} 份\n')
        fp.write(f'- 可疑 {len(findings)} 处\n\n')
        if findings:
            fp.write('| 文件 | 表头列名 | 数据行数 |\n| --- | --- | --- |\n')
            for x in findings:
                fp.write('| %s | %s | %d |\n'
                         % (os.path.basename(x['image']), x['header'], x['rows']))
            fp.write('\n> 表头声明了这一列，但整列数据都是空的。'
                     '需要人工确认是 OCR 漏读、打码、还是报告本身没印。\n')
    print()
    print('报告:', args.report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
