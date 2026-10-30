# -*- coding: utf-8 -*-
r"""30 本地：残缺项目名补全（规则层，闭集候选，不猜）

问题：
    报告单打码 / OCR 截断会让项目名残缺，例如
      '离甲状腺素'   ← 前面的"游"被涂掉
      '...离甲状腺素' ← 涂黑块被识别成 '...'
      '球比值'       ← '白/' 被涂掉
    这些行在评测里一律算未命中，但信息其实还在（结果/单位/参考区间都完好）。

用法：
    python tools\30_complete_names.py --predictions predictions.jsonl ^
        --vocab vocab_noleak.json --out predictions_completed.jsonl"""

import os
import re
import sys
import json
import difflib
import argparse
import collections

sys.stdout.reconfigure(encoding='utf-8')

ANCHOR_RE = re.compile(r'(\.{2,}|…+|·{2,}|＿+|_{2,})')
TRAIL_PUNCT_RE = re.compile(r'[\s。，,；;：:、]+$')
LEAD_PUNCT_RE = re.compile(r'^[\s。，,；;：:、]+')
LATIN_FRAG_RE = re.compile(r'[A-Za-z][A-Za-z0-9\-/#\.]{2,}')


def latin_frags(name):
    """抽出名称里的拉丁片段（'抗EB病毒衣壳抗原IgA抗体' → {'iga'}）。

    打码一般只涂中文，'[anti-SSA]'、'VCL'、'IgA' 这类拉丁缩写常常完整保留，
    是很强的锚点：能用它反查词库里的唯一中文名。
    """
    return {f.lower().replace('.', '').replace('/', '') for f in LATIN_FRAG_RE.findall(name or '')}


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--predictions', required=True)
    ap.add_argument('--vocab', required=True)
    ap.add_argument('--out', default='predictions_completed.jsonl')
    ap.add_argument('--log', default='')
    ap.add_argument('--fuzzy', type=float, default=0.85,
                    help='仅 --allow-unanchored 时生效的编辑距离阈值')
    ap.add_argument('--allow-unanchored', action='store_true',
                    help='允许对没有锚点的短名字做模糊改写（默认关闭：实测误伤率极高）')
    ap.add_argument('--min-stem', type=int, default=2)
    ap.add_argument('--dry-run', action='store_true')
    return ap.parse_args()


def norm_key(name):
    s = '' if name is None else str(name)
    s = s.replace('（', '(').replace('）', ')').lower().replace(' ', '')
    for a, b in (('：', ':'), ('－', '-'), ('—', '-'), ('–', '-'), ('~', '-'), ('～', '-')):
        s = s.replace(a, b)
    return s


class Completer:
    def __init__(self, vocab_path, fuzzy=0.85, min_stem=2, allow_unanchored=False):
        v = json.load(open(vocab_path, encoding='utf-8'))
        self.names = v['names']
        self.keys = v.get('keys') or {}
        if not self.keys:
            for n in self.names:
                self.keys.setdefault(norm_key(n), []).append(n)
        self.fuzzy = fuzzy
        self.min_stem = min_stem
        self.allow_unanchored = allow_unanchored
        # 预建后缀/前缀索引，避免每次都线性扫 2917 条
        self.by_suffix = collections.defaultdict(list)
        self.by_prefix = collections.defaultdict(list)
        self.by_latin = collections.defaultdict(set)
        for k, cands in self.keys.items():
            for name in cands:
                for L in range(2, min(len(k), 8) + 1):
                    self.by_suffix[k[-L:]].append(name)
                    self.by_prefix[k[:L]].append(name)
                for frag in latin_frags(name):
                    self.by_latin[frag].add(name)

    def complete(self, raw):
        """返回 (补全后的名字, 原因, 候选)。

        只处理有明确锚点的情况。无锚点时一律不改——原因见文件头的"为什么不做模糊匹配"。
        """
        if not raw:
            return raw, 'empty', ''
        s = str(raw).replace('\\n', ' ').replace('\\t', ' ')
        s = s.replace('（', '(').replace('）', ')')
        s = s.strip()
        if not s:
            return raw, 'empty', ''

        has_lead = bool(re.match(r'^(\.{2,}|…+|·{2,}|＿+|_+)', s))
        has_trail = bool(re.search(r'(\.{2,}|…+|·{2,}|＿+|_+)$', s))
        stem = ANCHOR_RE.sub('', s).strip()
        if not stem:
            return s, 'only_anchor', ''

        skey = norm_key(stem)
        if len(skey) < self.min_stem:
            return s, 'stem_too_short', ''

        if not has_lead and not has_trail:
            # 没有 '...' 锚点：一律不动。
            #
            # 实测教训：曾试过"无锚点时用拉丁缩写反查词库"，在 MedRepBench 上
            if skey in self.keys:
                return s, 'already_valid', ''
            if self.allow_unanchored:
                return self._fuzzy(skey, s)
            return s, 'no_anchor', ''

        # 有锚点：锚点那一侧的字确定被涂掉了，另一侧可信
        if has_lead and not has_trail:
            cands = [c for c in self.by_suffix.get(skey[-min(len(skey), 8):], [])
                     if norm_key(c).endswith(skey) and len(norm_key(c)) > len(skey)]
            reason = 'lead_anchor'
        elif has_trail and not has_lead:
            cands = [c for c in self.by_prefix.get(skey[:min(len(skey), 8)], [])
                     if norm_key(c).startswith(skey) and len(norm_key(c)) > len(skey)]
            reason = 'trail_anchor'
        else:
            # 两侧都有锚点：中间还有一段可信文字，要求候选包含它
            cands = [c for c in self.names
                     if skey in norm_key(c) and len(norm_key(c)) > len(skey)]
            reason = 'both_anchor'

        uniq = sorted({norm_key(c): c for c in cands}.values())
        if len(uniq) == 1:
            return uniq[0], reason, uniq[0]
        if len(uniq) > 1:
            # 多个候选：再要求"残缺串在候选里的位置与锚点一致"
            tight = [c for c in uniq if self._anchor_consistent(skey, norm_key(c), has_lead, has_trail)]
            tight = sorted({norm_key(c): c for c in tight}.values())
            if len(tight) == 1:
                return tight[0], reason + '_tight', tight[0]
            # 仍有多个：先用拉丁缩写收窄（'[anti-SSA]' 这类通常没被涂掉）
            frags = latin_frags(s)
            if frags:
                narrowed = [c for c in tight or uniq
                            if frags & latin_frags(c)]
                narrowed = sorted({norm_key(c): c for c in narrowed}.values())
                if len(narrowed) == 1:
                    return narrowed[0], reason + '_latin', narrowed[0]
                if narrowed:
                    uniq = narrowed
            # 还不行：锚点已确定"这一侧被涂掉"，补得越少越可能是对的
            # （'...离甲状腺素' → 游离甲状腺素 / 血清游离甲状腺素，取短的）
            pool = uniq
            shortest = sorted(pool, key=lambda c: (len(norm_key(c)), c))
            if len(shortest) >= 2 and len(norm_key(shortest[0])) < len(norm_key(shortest[1])):
                return shortest[0], reason + '_shortest', shortest[0]
            return s, 'ambiguous', '|'.join(pool[:4])
        return s, 'no_candidate', ''

    def _latin_anchor(self, s, skey):
        """用拉丁缩写反查唯一中文名。

        只补不删：候选必须不短于输入。否则 '抗PM-Scl抗体 [PM-SCL]'
        会被改写成更短的 '抗PM-Scl抗体'，把报告里有的信息丢掉。
        """
        frags = latin_frags(s)
        if not frags:
            return None
        for frag in sorted(frags, key=len, reverse=True):
            cands = [c for c in (self.by_latin.get(frag) or set())
                     if len(norm_key(c)) >= len(skey)]
            if not cands:
                continue
            uniq = sorted({norm_key(c): c for c in cands}.values())
            if len(uniq) == 1:
                cand = uniq[0]
                # 防止 'IgA' 这种到处出现的缩写把无关项目拽过来：
                # 要求残缺名的中文主干与候选中文主干有足够重合
                if self._cn_overlap(skey, norm_key(cand)) >= 0.5:
                    return cand, 'latin_anchor'
                continue
            # 多候选：用中文重合度挑，必须明显领先
            scored = sorted(((self._cn_overlap(skey, norm_key(c)), c) for c in uniq),
                            reverse=True)
            if len(scored) >= 2 and scored[0][0] - scored[1][0] >= 0.25 and scored[0][0] >= 0.6:
                return scored[0][1], 'latin_anchor_scored'
        return None

    @staticmethod
    def _cn_overlap(a, b):
        """两串里中文字符集合的重合度（Jaccard）。"""
        ca = {ch for ch in a if '\u4e00' <= ch <= '\u9fff'}
        cb = {ch for ch in b if '\u4e00' <= ch <= '\u9fff'}
        if not ca or not cb:
            return 0.0
        return len(ca & cb) / len(ca | cb)

    @staticmethod
    def _anchor_consistent(skey, ckey, has_lead, has_trail):
        """残缺串必须贴在候选的锚点那一侧（前缀锚点→贴在尾部）。"""
        if has_lead and not has_trail:
            return ckey.endswith(skey)
        if has_trail and not has_lead:
            return ckey.startswith(skey)
        return skey in ckey

    def _fuzzy(self, skey, s):
        """无锚点时的模糊匹配，默认关闭（--allow-unanchored 才启用，仅供对照实验）。"""
        best, best_r, tie = '', 0.0, 0
        for c in self.names:
            ck = norm_key(c)
            if abs(len(ck) - len(skey)) > 2:
                continue
            r = difflib.SequenceMatcher(None, skey, ck).ratio()
            if r > best_r + 1e-9:
                best, best_r, tie = c, r, 1
            elif abs(r - best_r) <= 1e-9 and c != best:
                tie += 1
        if best and best_r >= self.fuzzy and tie == 1:
            return best, 'fuzzy', best
        if best and best_r >= self.fuzzy:
            return s, 'fuzzy_tie', best
        return s, 'no_candidate', ''


def main():
    args = parse_args()
    comp = Completer(args.vocab, fuzzy=args.fuzzy, min_stem=args.min_stem,
                     allow_unanchored=args.allow_unanchored)

    rows = [json.loads(l) for l in open(args.predictions, encoding='utf-8') if l.strip()]
    reasons = collections.Counter()
    changes = []

    for r in rows:
        for it in r.get('items') or []:
            old = it.get('item_name') or ''
            new, reason, cand = comp.complete(old)
            reasons[reason] += 1
            if new != old:
                changes.append({'image': r.get('image', ''), 'before': old,
                                'after': new, 'reason': reason, 'cand': cand})
                it['item_name'] = new

    if not args.dry_run:
        with open(args.out, 'w', encoding='utf-8') as fo:
            for r in rows:
                fo.write(json.dumps(r, ensure_ascii=False) + '\n')

    total = sum(reasons.values())
    print(f'项目 {total} 个')
    for k, v in reasons.most_common():
        print(f'  {k:<18} {v:>4}  ({v/total*100:>5.1f}%)')
    print()
    print(f'实际改写 {len(changes)} 处')
    for c in changes:
        print(f"  [{c['reason']}] {c['before']!r} → {c['after']!r}")
    if not args.dry_run:
        print(f'\n输出: {args.out}')
        log = args.log or (os.path.splitext(args.out)[0] + '_changes.md')
        with open(log, 'w', encoding='utf-8') as fp:
            fp.write('# 残缺名补全变更日志\n\n')
            fp.write(f'- 输入 `{args.predictions}`\n- 词库 `{args.vocab}`\n')
            fp.write(f'- 项目 {total} 个，改写 {len(changes)} 处\n\n')
            fp.write('## 原因分布\n\n| 原因 | 数量 |\n| --- | --- |\n')
            for k, v in reasons.most_common():
                fp.write(f'| {k} | {v} |\n')
            fp.write('\n## 改写明细\n\n| 原因 | 改前 | 改后 | 样本 |\n| --- | --- | --- | --- |\n')
            for c in changes:
                fp.write(f"| {c['reason']} | `{c['before']}` | `{c['after']}` | "
                         f"{c['image']} |\n")
        print(f'日志: {log}')


if __name__ == '__main__':
    main()
