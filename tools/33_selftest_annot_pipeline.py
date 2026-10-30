# -*- coding: utf-8 -*-
r"""33 本地：标注链路自检（不依赖浏览器，验证数据格式契约）

为什么需要：
    32_make_review_tool.py 生成的是 HTML，真正的标注发生在浏览器里，
    Python 这边测不到。但"导出的 JSON → 23 生成 gold"这一段是纯数据转换，
    可以而且必须自动验证——否则一旦格式对不上，人工标了半天的数据就白费了。

用法：
    python tools\33_selftest_annot_pipeline.py"""

import os
import re
import sys
import json
import glob
import shutil
import tempfile
import importlib.util

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
OK = '[ OK ]'
FAIL = '[FAIL]'
_fails = []


def check(cond, msg, detail=''):
    if cond:
        print('%s %s' % (OK, msg))
    else:
        print('%s %s' % (FAIL, msg))
        if detail:
            print('       %s' % detail)
        _fails.append(msg)
    return cond


def load_tool(fname, modname):
    path = os.path.join(HERE, fname)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 造数据：模拟 32 的导出 ----------
def make_export():
    return {
        'version': 2,
        'exported_at': '2026-10-28T10:00:00',
        'note': 'selftest',
        'samples': [
            {
                'report_id': 'sample_a',
                'source_image': 'sample_a.png',
                'masked_image': 'sample_a_masked.png',
                'masks': [{'x': 1, 'y': 2, 'w': 3, 'h': 4}],
                '_done': True,
                'items': [
                    # 人工确认了规则的预填（一致）
                    {'name': '甘油三酯', 'value': '1.89', 'unit': 'mmol/L',
                     'range_raw': '0.00~1.70', 'flag': 'abnormal',
                     'rule_judged': 'abnormal', 'rule_conf': 'high'},
                    # 人工推翻了规则的预填
                    {'name': '总胆固醇', 'value': '2.96', 'unit': 'mmol/L',
                     'range_raw': '3.00~5.70', 'flag': 'normal',
                     'rule_judged': 'abnormal', 'rule_conf': 'high'},
                    # 规则拒判，人工判了
                    {'name': '平均血小板体积', 'value': '8.2', 'unit': 'fL',
                     'range_raw': '', 'flag': 'normal',
                     'rule_judged': '', 'rule_conf': ''},
                    # 人工也没判 → unknown
                    {'name': '某定性项', 'value': 'Negative', 'unit': '',
                     'range_raw': 'Negative', 'flag': '',
                     'rule_judged': '', 'rule_conf': 'high'},
                    # 空名称行，应被丢弃
                    {'name': '  ', 'value': '1', 'unit': '', 'range_raw': '',
                     'flag': '', 'rule_judged': '', 'rule_conf': ''},
                ],
            },
            {
                'report_id': 'sample_b',
                'source_image': 'sample_b.png',
                'masked_image': 'sample_b_masked.png',
                'masks': [],
                '_done': False,
                'items': [
                    {'name': '白细胞计数(WBC)', 'value': '40.79', 'unit': '10^9/L',
                     'range_raw': '4.4--11.9', 'flag': 'abnormal',
                     'rule_judged': 'abnormal', 'rule_conf': 'high'},
                    {'name': '血小板计数(PLT)', 'value': '148.00', 'unit': '10^9/L',
                     'range_raw': '188--472', 'flag': 'abnormal',
                     'rule_judged': 'abnormal', 'rule_conf': 'high'},
                ],
            },
        ],
    }


# ---------- 造数据：模拟旧版逐张 *_annot.json ----------
def make_legacy():
    return {
        'report_id': 'legacy_x',
        'source_image': 'legacy_x.png',
        'masked_image': 'legacy_x_masked.png',
        'masks': [],
        'items': [
            {'name': '总蛋白', 'value': '73.4', 'unit': 'g/L',
             'range_raw': '65--85'},
        ],
    }


def main():
    t23 = load_tool('23_gold_from_annotation.py', 't23')

    tmp = tempfile.mkdtemp(prefix='annot_selftest_')
    try:
        # 造两张 1x1 PNG，让"图片收拢"那段也走通（否则会刷一堆"未找到"警告）
        png_1px = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\xdac\xfc'
            b'\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00'
            b'IEND\xaeB`\x82')
        for name in ('sample_a_masked.png', 'sample_b_masked.png',
                     'legacy_x_masked.png'):
            with open(os.path.join(tmp, name), 'wb') as f:
                f.write(png_1px)

        # ---- 1. 打包格式能被读 ----
        exp_path = os.path.join(tmp, 'review_export_20261028.json')
        json.dump(make_export(), open(exp_path, 'w', encoding='utf-8'),
                  ensure_ascii=False)
        samples = t23.load_samples(exp_path)
        check(len(samples) == 2, '打包格式能读出 2 份样本',
              '实际 %d 份' % len(samples))
        ids = sorted(s['report_id'] for s in samples)
        check(ids == ['sample_a', 'sample_b'], 'report_id 正确', str(ids))

        # ---- 2. 旧格式仍兼容，且能与打包格式共存 ----
        leg_path = os.path.join(tmp, 'legacy_x_annot.json')
        json.dump(make_legacy(), open(leg_path, 'w', encoding='utf-8'),
                  ensure_ascii=False)
        mixed = t23.load_samples(tmp)
        check(len(mixed) == 3, '旧格式 + 打包格式能共存（共 3 份）',
              '实际 %d 份: %s' % (len(mixed), [s['report_id'] for s in mixed]))

        # ---- 3. flag 三态 ----
        check(t23.norm_flag('normal') == 'normal', 'norm_flag 接受 normal')
        check(t23.norm_flag('Abnormal') == 'abnormal', 'norm_flag 大小写不敏感')
        check(t23.norm_flag('') == 'unknown', 'flag 为空 → unknown（不臆造）')
        check(t23.norm_flag(None) == 'unknown', 'flag 为 None → unknown')
        check(t23.norm_flag('阳性') == 'unknown', 'flag 非法值 → unknown')

        # ---- 4. 参考区间 raw 保留 ----
        ref = t23.parse_reference('4.4--11.9')
        check(ref.get('raw') == '4.4--11.9',
              'reference.raw 原样保留（区间评测靠它逐字比较）', str(ref))
        check(ref.get('low') == '4.4' and ref.get('high') == '11.9',
              '双横线区间正确解析', str(ref))

        # ---- 5. 跑一次完整转换，检查 gold 落盘内容 ----
        out_dir = os.path.join(tmp, 'out')
        old_argv = sys.argv
        try:
            sys.argv = ['23', '--src', tmp, '--out', out_dir]
            t23.main()
        finally:
            sys.argv = old_argv

        gold_dir = os.path.join(out_dir, 'gold')
        files = [f for f in glob.glob(os.path.join(gold_dir, '*.json'))
                 if os.path.basename(f) != 'manifest.json']
        check(len(files) == 3, 'gold 写出 3 份', '实际 %d 份' % len(files))

        ga = json.load(open(os.path.join(gold_dir, 'sample_a.json'),
                            encoding='utf-8'))
        check(len(ga['items']) == 4, '空名称行被丢弃（5 行 → 4 项）',
              '实际 %d 项' % len(ga['items']))
        flags = [it['flag'] for it in ga['items']]
        check(flags == ['abnormal', 'normal', 'normal', 'unknown'],
              'flag 三态正确落盘', str(flags))
        check(ga['items'][0].get('rule_prefill') == {'flag': 'abnormal',
                                                     'conf': 'high'},
              'rule_prefill 追溯信息保留', str(ga['items'][0].get('rule_prefill')))
        check('rule_prefill' not in ga['items'][2],
              '规则未预填的项不带 rule_prefill')

        gx = json.load(open(os.path.join(gold_dir, 'legacy_x.json'),
                            encoding='utf-8'))
        check(gx['items'][0]['flag'] == 'unknown',
              '旧格式没有 flag 字段 → unknown', str(gx['items'][0]['flag']))
        check(gx['items'][0]['reference'].get('raw') == '65--85',
              '旧格式的区间 raw 也保留')

        man = json.load(open(os.path.join(gold_dir, 'manifest.json'),
                             encoding='utf-8'))
        check(man['count'] == 3, 'manifest 份数正确')
        check(all('image' in s and 'report_id' in s for s in man['samples']),
              'manifest 每条都带 report_id + image')

        # ---- 6. 32 生成的 HTML 结构（如果已经生成过） ----
        html_path = r'<数据目录>\标注工具\review_tool.html'
        if os.path.isfile(html_path):
            html = open(html_path, encoding='utf-8').read()
            check('__IMAGES__' not in html and '__RESUME__' not in html,
                  '复核工具里没有残留占位符')
            for fn in ('function exportAll', 'function importState',
                       'function save()', 'function restore()',
                       'function renderItems'):
                check(fn in html, '复核工具含 %s' % fn)
            check('localStorage' in html,
                  '复核工具接入了 localStorage 断点续标')
        else:
            print('       （跳过 HTML 检查：还没生成复核工具）')

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print('=' * 60)
    if _fails:
        print('失败 %d 项：' % len(_fails))
        for f in _fails:
            print('  - ' + f)
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
