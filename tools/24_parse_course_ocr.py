# -*- coding: utf-8 -*-
r"""24 本地：解析课程版 OCRDocument（blocks/cells）并抽取字段

为什么单独写这个：
    课程版的 artifacts/ocr/*.ocr.json 与 MedRepBench 的 artifacts 格式不同：
      课程版: {blocks: [{block_type, cells: [{row, col, text, is_header}]}]}
      MedRepBench: {results: [{parsing_res_list: [字符串]}]}

用法：
    python tools\24_parse_course_ocr.py
    python tools\24_parse_course_ocr.py --src "<上一项目>\artifacts\ocr" """

import os
import sys
import json
import glob
import re
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--src', default=r'<上一项目>\artifacts\ocr')
    ap.add_argument('--out', default='course_ocr_items.json')
    return ap.parse_args()


def rebuild_tables(doc):
    """用 cells 的 row/col 重建二维表。"""
    tables = []
    for b in doc.get('blocks', []):
        if b.get('block_type') != 'table':
            continue
        cells = b.get('cells') or []
        if not cells:
            continue
        # 收集行号列号
        rows = collections.defaultdict(dict)
        max_col = 0
        for c in cells:
            r = c.get('row')
            col = c.get('col')
            if r is None or col is None:
                continue
            rows[r][col] = (c.get('text') or '').strip()
            max_col = max(max_col, col)
        if not rows:
            continue
        grid = []
        for r in sorted(rows):
            grid.append([rows[r].get(c, '') for c in range(max_col + 1)])
        tables.append({'grid': grid, 'bbox': b.get('bbox')})
    return tables


def find_header(grid):
    """找表头行，返回 {字段: 列索引}"""
    keys = {
        'item_name': ['项目名称', '项目', '检验项目', '检查项目', '名称'],
        'item_value': ['检查结果', '结果', '检验结果', '测定值'],
        'item_unit': ['单位', '计量单位'],
        'item_range': ['参考范围', '参考值', '参考区间', '正常范围'],
    }
    best = None
    for i, row in enumerate(grid[:6]):
        idx = {}
        for ci, cell in enumerate(row):
            c = cell.replace(' ', '')
            for f, kws in keys.items():
                if f in idx:
                    continue
                if any(k in c for k in kws):
                    idx[f] = ci
                    break
        if len(idx) > (len(best[1]) if best else 0):
            best = (i, idx)
    return best


CN_RE = re.compile(r'[\u4e00-\u9fff]')
NUM_ONLY = re.compile(r'^\d{1,3}$')
LATEX_RE = re.compile(r'\$\s*\\?[a-zA-Z{}^_]*\s*\$?')


def clean_text(s):
    """清理 OCR 残留的 LaTeX 标记与多余符号。
    注意：只在行首删 '^'，单位列的 10^9/L 必须保留。"""
    s = (s or '').strip()
    s = s.replace('\\uparrow', '↑').replace('\\downarrow', '↓')
    s = s.replace('\\mu', 'μ').replace('\\times', '×')
    # 顺序很重要：先去掉 $ 与 {}，再清行首的 ^*，否则 ^ 前面挡着 $ 就清不掉
    s = re.sub(r'\$\s*', '', s)
    s = re.sub(r'[{}]+', '', s)
    s = re.sub(r'^[\^\*\s]+', '', s)
    s = s.replace('μ ', 'μ')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def extract_items(grid):
    """从二维表抽 items。含序号列偏移修正。"""
    h = find_header(grid)
    if not h:
        return [], None
    start, idx = h
    idx = dict(idx)

    data_rows = grid[start + 1:]
    maxc = max((len(r) for r in grid), default=0)

    # 序号列偏移修正：名称列若是纯数字、且右侧一列含中文，则名称列右移一格
    ni = idx.get('item_name')
    if ni is not None and data_rows:
        sample = [r[ni].strip() for r in data_rows[:5] if ni < len(r) and r[ni].strip()]
        if sample and all(NUM_ONLY.match(x) for x in sample):
            nxt = ni + 1
            if nxt < maxc:
                right = [r[nxt] for r in data_rows[:5] if nxt < len(r)]
                if any(CN_RE.search(x) for x in right):
                    idx['item_name'] = nxt

    items = []
    for row in data_rows:
        def cell(f):
            j = idx.get(f)
            if j is None or j >= len(row):
                return ''
            return clean_text(row[j])

        name = cell('item_name')
        if not name:
            continue
        if any(k in name for k in ('项目名称', '检测项目', '合计', '备注', '接收者', '注：')):
            continue
        items.append({
            'item_name': name,
            'item_value': cell('item_value'),
            'item_unit': cell('item_unit'),
            'item_range': cell('item_range'),
        })
    return items, idx


def main():
    args = parse_args()
    files = sorted(glob.glob(os.path.join(args.src, '*.ocr.json')))
    if not files:
        print(f'{args.src} 下没有 *.ocr.json')
        sys.exit(1)

    all_out = {}
    print('=' * 72)
    print('课程版 OCRDocument 解析验证')
    print('=' * 72)

    for f in files:
        doc = json.load(open(f, encoding='utf-8'))
        rid = doc.get('report_id') or os.path.basename(f)
        tables = rebuild_tables(doc)
        print(f'\n{rid}: {len(doc.get("blocks", []))} blocks, '
              f'{len(tables)} 个表格')

        sample_items = []
        for ti, tb in enumerate(tables):
            grid = tb['grid']
            items, idx = extract_items(grid)
            print(f'  表格[{ti}] {len(grid)}行 -> 抽出 {len(items)} 项'
                  f'{"  列映射=" + str(idx) if idx else "  (未识别表头)"}')
            sample_items.extend(items)

        all_out[rid] = sample_items
        # 打印前 8 项
        for it in sample_items[:8]:
            print(f"    {it['item_name'][:22]:<24}|{it['item_value'][:12]:<14}|"
                  f"{it['item_unit'][:12]:<14}|{it['item_range'][:18]}")
        if len(sample_items) > 8:
            print(f'    ... 共 {len(sample_items)} 项')
        # 关键检查：参考范围抽出率
        if sample_items:
            with_range = sum(1 for x in sample_items if x['item_range'])
            print(f'  参考范围非空: {with_range}/{len(sample_items)} '
                  f'({with_range/len(sample_items)*100:.1f}%)')

    with open(args.out, 'w', encoding='utf-8') as fo:
        json.dump(all_out, fo, ensure_ascii=False, indent=1)
    print(f'\n结果: {args.out}')

    print()
    print('=' * 72)
    print('判读')
    print('=' * 72)
    total = sum(len(v) for v in all_out.values())
    withr = sum(1 for v in all_out.values() for it in v if it['item_range'])
    print(f'  共抽出 {total} 项，其中参考范围非空 {withr} 项')
    print(f'  说明：这两份是标准单栏版式（项目名称/检查结果/单位/参考范围/提示），')
    print(f'        若抽出率接近 100%，则证实"MedRepBench 低分主要因数据打码"，')
    print(f'        而非解析器能力不足。')


if __name__ == '__main__':
    main()
