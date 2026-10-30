# -*- coding: utf-8 -*-
r"""29 本地：建检验项目词库（残缺名补全的候选集）

为什么需要：
    残缺名补全本质是"在受限候选集上做实体链接"，不是自由生成。
    给 LLM 一个候选闭集，它只能选不能编，幻觉就压住了；
    给规则层一个词库，它就能做确定性的编辑距离匹配。

用法：
    # 给 MedRepBench 那 20 份评测用（排除自己）
    python tools\29_build_vocab.py --exclude-from predictions.jsonl ^
        --out vocab_noleak.json"""

import os
import re
import sys
import json
import glob
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

DEFAULT_GOLD = r'<数据目录>\eval_gold_medrep'

# 括号里的缩写单独也收一份（'白细胞计数(WBC)' → 也收 'WBC'）
PAREN_RE = re.compile(r'[（(]([^）)]{1,30})[）)]')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--gold-dir', default=DEFAULT_GOLD)
    ap.add_argument('--exclude-from', default='',
                    help='predictions.jsonl 或图片目录；其中的样本不进入词库')
    ap.add_argument('--out', default='vocab.json')
    return ap.parse_args()


def norm(name):
    s = '' if name is None else str(name)
    s = s.replace('\\n', ' ').replace('\\t', ' ')
    s = s.replace('（', '(').replace('）', ')')
    s = re.sub(r'^\s*[\*＊★☆#＃]\s*', '', s)
    s = re.sub(r'(\.{2,}|…+|·{2,})', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def norm_key(name):
    """比较用的键：与 07_evaluate 的 normalize_text 对齐。"""
    s = norm(name).lower().replace(' ', '')
    for a, b in (('：', ':'), ('－', '-'), ('—', '-'), ('–', '-'), ('~', '-'), ('～', '-')):
        s = s.replace(a, b)
    return s


def load_exclude(path):
    """返回要排除的 report_id 集合。"""
    ids = set()
    if not path:
        return ids
    if os.path.isdir(path):
        for f in glob.glob(os.path.join(path, '*.json')):
            if os.path.basename(f).startswith('_'):
                continue
            try:
                rec = json.load(open(f, encoding='utf-8'))
            except (ValueError, OSError):
                continue
            img = rec.get('image') or os.path.basename(f)
            ids.add(os.path.splitext(os.path.basename(img))[0])
        return ids
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                o = json.loads(line)
                img = o.get('image') or o.get('report_id') or ''
                ids.add(os.path.splitext(os.path.basename(img))[0])
    return ids


def main():
    args = parse_args()
    exclude = load_exclude(args.exclude_from)

    counter = collections.Counter()
    used_files = 0
    for f in glob.glob(os.path.join(args.gold_dir, '*.json')):
        base = os.path.basename(f)
        if base in ('manifest.json', 'upload_manifest.json'):
            continue
        d = json.load(open(f, encoding='utf-8'))
        if d.get('report_id') in exclude:
            continue
        used_files += 1
        for it in d.get('items') or []:
            name = it.get('name') or it.get('item_name') or ''
            n = norm(name)
            if len(n) < 2:
                continue
            counter[n] += 1
            for m in PAREN_RE.findall(n):
                ab = norm(m)
                if 2 <= len(ab) <= 12:
                    counter[ab] += 1

    names = sorted(counter)
    vocab = {
        'source': 'MedRepBench 官方人工标注（CC BY-NC 4.0）',
        'built_from_files': used_files,
        'excluded_samples': len(exclude),
        'count': len(names),
        'names': names,
        'freq': {n: counter[n] for n in names},
        'keys': {},
    }
    for n in names:
        vocab['keys'].setdefault(norm_key(n), []).append(n)

    with open(args.out, 'w', encoding='utf-8') as fp:
        json.dump(vocab, fp, ensure_ascii=False, indent=1)

    print(f'词库条目 {len(names)}')
    print(f'  来源文件     {used_files}')
    print(f'  排除样本     {len(exclude)}')
    print(f'  唯一键       {len(vocab["keys"])}')
    print(f'输出: {args.out}')
    print()
    print('样例（出现频次最高的 10 条）:')
    for n, c in counter.most_common(10):
        print(f'  {c:>4}  {n}')


if __name__ == '__main__':
    main()
