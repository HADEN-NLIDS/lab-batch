# -*- coding: utf-8 -*-
r"""27 本地：复核并清洗自采数据的 gold（不改原文件，产出 gold_clean + 变更日志）

为什么需要这一步（不改就会把标注噪声算成算法错误）：
  1. 标注工具把 OCR 的异常标记原样带进了 unit 字段（'\uparrow'、'\downarrow'），
     这不是单位，会让 unit 全判错
  2. 数值里带着 '↑/↓'（'1.89 ↑'），与去掉了箭头的预测无法逐字相等
  3. 名称里带报告单的 '*'（自费项目）、全角括号 '（WBC）'、LaTeX 残留 '\gamma'
  4. 少量项目单位漏填——报告原图上是有的，标注时漏了

用法：
    python tools\27_clean_gold.py ^
        --gold-dir "<数据目录>\自采数据集\gold" ^
        --fill-from .\out ^
        --out-dir "<数据目录>\自采数据集\gold_clean" """

import os
import re
import sys
import json
import glob
import argparse

sys.stdout.reconfigure(encoding='utf-8')

ARROW_RE = re.compile(r'(\\(?:up|down)arrow|[↑↓])')
NAME_PREFIX_RE = re.compile(r'^\s*[\*＊★☆#＃]\s*')
GREEK = {'gamma': 'γ', 'alpha': 'α', 'beta': 'β', 'mu': 'μ', 'times': '×'}
LATEX_GREEK_RE = re.compile(r'\$\s*\\(gamma|alpha|beta|mu|times)\s*\$')
BARE_GREEK_RE = re.compile(r'\\(gamma|alpha|beta|mu|times)')
DROP_NAMES = {'序号', '编号', '项目', '项目名称', '检验项目', '代码'}

UNIT_FULL = re.compile(
    r'^('
    r'%'
    r'|(?:×\s?)?10(?:\^?\d+|E\d+)\s*[/／]?\s*[a-zA-Zμ]*'
    r'|[a-zA-Zμ]+[/／][a-zA-Z0-9]+'
    r'|[a-zA-Zμ]{1,6}'
    r')$'
)
WATERMARK_RE = re.compile(r'^.{1,6}HR\s*\d{0,3}$')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--gold-dir', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--fill-from', default='', help='OCR artifacts 目录，用于补单位')
    ap.add_argument('--log', default='', help='变更日志路径（默认写在 out-dir 下）')
    return ap.parse_args()


def clean_name(s):
    s = '' if s is None else str(s)
    s = NAME_PREFIX_RE.sub('', s)
    s = LATEX_GREEK_RE.sub(lambda m: GREEK[m.group(1)], s)
    s = BARE_GREEK_RE.sub(lambda m: GREEK[m.group(1)], s)
    s = s.replace('（', '(').replace('）', ')')
    s = ARROW_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip()


def clean_scalar(s):
    s = '' if s is None else str(s)
    s = LATEX_GREEK_RE.sub(lambda m: GREEK[m.group(1)], s)
    s = BARE_GREEK_RE.sub(lambda m: GREEK[m.group(1)], s)
    s = ARROW_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip()


def clean_unit(s):
    """单位里的箭头是异常标记、'*' 是自费标记，都不是单位本体。"""
    s = ARROW_RE.sub('', '' if s is None else str(s))
    s = NAME_PREFIX_RE.sub('', s)
    return clean_scalar(s)


def ocr_unit_index(path):
    """从 OCR 结果里建 name -> 单位 的映射（读报告原文的表格行，不用解析器结果）。"""
    if not path or not os.path.isdir(path):
        return {}
    mapping = {}
    for f in glob.glob(os.path.join(path, '*.json')):
        if os.path.basename(f).startswith('_'):
            continue
        txt = open(f, encoding='utf-8').read()
        per_file = {}
        for row in re.findall(r'<tr>(.*?)</tr>', txt, re.S):
            cells = [re.sub(r'<[^>]+>', '', c).strip()
                     for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
            cells = [clean_scalar(clean_cell_light(c)) for c in cells]
            if len(cells) < 3:
                continue
            # 跳过行首序号/水印列
            while cells and (WATERMARK_RE.match(cells[0]) or re.fullmatch(r'\d{1,2}', cells[0])):
                cells = cells[1:]
            if len(cells) < 3:
                continue
            name = clean_name(cells[0])
            if not name or name in DROP_NAMES:
                continue
            # 名称之后第一个像单位的格就是单位
            unit = ''
            for c in cells[1:]:
                c2 = clean_scalar(c)
                if c2 and len(c2) <= 14 and not re.search(r'[\u4e00-\u9fff]', c2) \
                        and UNIT_FULL.match(c2):
                    unit = c2
                    break
            if unit:
                per_file.setdefault(name, unit)
        key = os.path.splitext(os.path.basename(f))[0]
        mapping[key] = per_file
    return mapping


def clean_cell_light(s):
    """只处理 OCR 常见的上下标写法，其余保持原样。"""
    s = re.sub(r'\^\{\s*(\d+)\s*\}', r'^\1', s)
    s = re.sub(r'\$\s*([^$]*?)\s*\$', r'\1', s)
    s = s.replace('\\times', '×').replace('\\mu', 'μ')
    s = re.sub(r'[{}]', '', s)
    return s.strip()


def main():
    args = parse_args()
    files = [f for f in glob.glob(os.path.join(args.gold_dir, '*.json'))
             if os.path.basename(f) != 'manifest.json']
    if not files:
        print(f'{args.gold_dir} 下没有 gold json')
        sys.exit(1)
    os.makedirs(args.out_dir, exist_ok=True)
    unit_map = ocr_unit_index(args.fill_from)

    changes = []
    total_in = total_out = 0
    filled = 0

    for f in sorted(files):
        d = json.load(open(f, encoding='utf-8'))
        rid = d['report_id']
        per_file_units = unit_map.get(rid, {})
        new_items = []
        for it in d['items']:
            total_in += 1
            old_name = it.get('name') or ''
            name = clean_name(old_name)
            if not name or name in DROP_NAMES:
                changes.append({'file': rid, 'kind': 'drop_row', 'before': old_name, 'after': ''})
                continue
            if name != old_name:
                changes.append({'file': rid, 'kind': 'name', 'before': old_name, 'after': name})
            it['name'] = name

            old_unit = it.get('unit') or ''
            unit = clean_unit(old_unit)
            if unit != old_unit:
                changes.append({'file': rid, 'kind': 'unit_clean', 'item': name,
                                'before': old_unit, 'after': unit})
            if not unit and per_file_units.get(name):
                unit = per_file_units[name]
                filled += 1
                changes.append({'file': rid, 'kind': 'unit_fill', 'item': name,
                                'before': '', 'after': unit})
            it['unit'] = unit

            old_val = it.get('value') or ''
            val = clean_scalar(old_val)
            if val != old_val:
                changes.append({'file': rid, 'kind': 'value', 'item': name,
                                'before': old_val, 'after': val})
            it['value'] = val

            new_items.append(it)
            total_out += 1
        d['items'] = new_items
        d['note'] = (d.get('note') or '') + ' 经 27_clean_gold 机械清洗（见变更日志）。'
        with open(os.path.join(args.out_dir, rid + '.json'), 'w', encoding='utf-8') as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)

    man_src = os.path.join(args.gold_dir, 'manifest.json')
    man = {'count': len(files), 'samples': []}
    if os.path.isfile(man_src):
        man = json.load(open(man_src, encoding='utf-8'))
        for s in man.get('samples', []):
            p = os.path.join(args.out_dir, s['report_id'] + '.json')
            if os.path.isfile(p):
                s['num_items'] = len(json.load(open(p, encoding='utf-8'))['items'])
    with open(os.path.join(args.out_dir, 'manifest.json'), 'w', encoding='utf-8') as fp:
        json.dump(man, fp, ensure_ascii=False, indent=2)

    log_path = args.log or os.path.join(args.out_dir, '清洗日志.md')
    by_kind = {}
    for c in changes:
        by_kind.setdefault(c['kind'], []).append(c)
    with open(log_path, 'w', encoding='utf-8') as fp:
        fp.write('# 自采数据 gold 复核清洗日志\n\n')
        fp.write(f'- 输入：`{args.gold_dir}`\n')
        fp.write(f'- 输出：`{args.out_dir}`\n')
        fp.write(f'- 项目数：{total_in} → {total_out}（删除 {total_in - total_out} 行）\n')
        fp.write(f'- 补全单位：{filled} 处\n\n')
        titles = {'drop_row': '删除的非数据行', 'name': '名称规范化',
                  'unit_clean': '单位清洗（去异常箭头/*）', 'unit_fill': '单位补全',
                  'value': '数值清洗（去 ↑↓）'}
        for kind in ('drop_row', 'name', 'unit_clean', 'unit_fill', 'value'):
            if kind not in by_kind:
                continue
            fp.write(f'## {titles[kind]}（{len(by_kind[kind])}）\n\n')
            fp.write('| 样本 | 项目 | 清洗前 | 清洗后 |\n| --- | --- | --- | --- |\n')
            for c in by_kind[kind]:
                fp.write('| %s | %s | `%s` | `%s` |\n' % (
                    c['file'], c.get('item', ''), c.get('before', ''), c.get('after', '')))
            fp.write('\n')

    print(f'读入 {total_in} 项 → 输出 {total_out} 项')
    for kind, lst in by_kind.items():
        print(f'  {kind}: {len(lst)}')
    print(f'日志: {log_path}')
    print(f'输出: {args.out_dir}')


if __name__ == '__main__':
    main()
