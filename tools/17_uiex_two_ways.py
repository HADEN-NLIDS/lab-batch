# -*- coding: utf-8 -*-
"""17 云端：过 show_log 这道关，两种方案并行试

新问题（2026-10-27 第8轮）：
    paddlenlp/utils/doc_parser.py:265
        self.ocr_infer_model = PaddleOCR(show_log=False, lang=self.ocr_lang)
    ValueError: Unknown argument: show_log
    → paddlenlp 2.8 传的参数在 paddleocr 3.7 里已被移除

用法：
    python tools/17_uiex_two_ways.py --mode a     # 试方案A
    python tools/17_uiex_two_ways.py --mode b     # 试方案B
    python tools/17_uiex_two_ways.py --mode both  # 都试（默认）"""

import os
import sys
import json
import re
import glob
import argparse
import importlib

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')

BASE = os.path.expanduser(
    '~/.paddlenlp/taskflow/information_extraction/uie-x-base/static')


def install_base_patches():
    """前面几轮已定位的三个补丁。"""
    fake = os.path.join(BASE, 'inference.pdmodel')
    if os.path.isfile(fake):
        os.remove(fake)
    try:
        hub = importlib.import_module('aistudio_sdk.hub')
        if not hasattr(hub, 'download'):
            hub.download = lambda *a, **k: None
    except Exception:
        pass
    import paddle
    if not getattr(paddle.inference, '_uiex_path_patched', False):
        _o = paddle.inference.Config

        def _c(mf, pf=None):
            if isinstance(mf, str) and mf.endswith('.pdmodel'):
                cand = mf[:-len('.pdmodel')] + '.json'
                if os.path.isfile(cand):
                    mf = cand
            return _o(mf, pf)
        paddle.inference.Config = _c
        paddle.inference._uiex_path_patched = True
    if not getattr(paddle.inference, '_uiex_memopt_patched', False):
        _oc = paddle.inference.create_predictor

        def _p(config):
            for n, a in (('disable_memory_optim', ()),
                         ('enable_memory_optim', (False,)),
                         ('switch_ir_optim', (False,))):
                f = getattr(config, n, None)
                if callable(f):
                    try:
                        f(*a)
                    except Exception:
                        pass
            return _oc(config)
        paddle.inference.create_predictor = _p
        paddle.inference._uiex_memopt_patched = True


def patch_show_log():
    """方案A：伪造 PaddleOCR 类，吞掉 show_log。"""
    try:
        import paddlenlp.utils.doc_parser as dp
    except Exception as e:
        print('  !! 无法导入 doc_parser:', e)
        return False
    if getattr(dp, '_uiex_ocr_shimmed', False):
        return True
    if not hasattr(dp, 'PaddleOCR'):
        print('  !! doc_parser 里没有 PaddleOCR 名字，patch 无法生效')
        return False
    _Orig = dp.PaddleOCR

    class _Shim(_Orig):
        def __init__(self, *a, **kw):
            kw.pop('show_log', None)     # 3.x 已移除该参数
            super().__init__(*a, **kw)

    dp.PaddleOCR = _Shim
    dp._uiex_ocr_shimmed = True
    print('  [patch4] 已伪造 doc_parser.PaddleOCR（吞掉 show_log）')
    return True


def load_artifact_layout(artifacts_dir, image_name):
    """从 artifacts 构造 UIE-X 的 layout 输入：[(bbox, text), ...]"""
    cands = glob.glob(os.path.join(artifacts_dir, '*.json'))
    for f in cands:
        if os.path.basename(f).startswith('_'):
            continue
        rec = json.load(open(f, encoding='utf-8'))
        if rec.get('image') != image_name:
            continue
        layout = []
        for res in (rec.get('results') or []):
            for s in (res.get('parsing_res_list') or []):
                if not isinstance(s, str) or 'label:' not in s:
                    continue
                m_lab = re.search(r'label:\s*(\S+)', s)
                m_box = re.search(r'bbox:\s*\[([^\]]*)\]', s)
                m_con = re.search(r'content:\s*(.*)', s, re.S)
                if not m_box:
                    continue
                try:
                    box = [float(x) for x in m_box.group(1).split(',')]
                except ValueError:
                    continue
                content = (m_con.group(1).strip() if m_con else '')
                # HTML 表格转纯文本：保留单元格分隔，供模型阅读
                text = re.sub(r'</t[dh]>', ' | ', content, flags=re.I)
                text = re.sub(r'</tr>', '\n', text, flags=re.I)
                text = re.sub(r'<[^>]+>', '', text)
                text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>')
                text = re.sub(r'[ \t]+', ' ', text).strip()
                if text:
                    layout.append((box, text))
        return layout
    return None


def build_ie(schema):
    from paddlenlp import Taskflow
    return Taskflow('information_extraction', schema=schema,
                    model='uie-x-base', ocr_lang='ch', schema_lang='ch')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mode', default='both', choices=['a', 'b', 'both'])
    ap.add_argument('--images', default='images')
    ap.add_argument('--artifacts', default='artifacts')
    ap.add_argument('--out', default='uiex_two_ways.json')
    args = ap.parse_args()

    print('=' * 64)
    print('0. 基础补丁')
    print('=' * 64)
    install_base_patches()
    print('  已装：Config路径替换 + create_predictor优化开关')

    imgs = []
    for ext in ('*.jpg', '*.jpeg', '*.png'):
        imgs.extend(sorted(glob.glob(os.path.join(args.images, ext))))
    if not imgs:
        print(f'!! {args.images} 下没有图片')
        sys.exit(1)
    img = imgs[0]
    img_name = os.path.basename(img)
    print(f'  测试图: {img}')

    results = {}

    if args.mode in ('a', 'both'):
        print()
        print('=' * 64)
        print('方案A：伪造 PaddleOCR，让 UIE-X 自己 OCR')
        print('=' * 64)
        if patch_show_log():
            try:
                ie = build_ie('检验项目')
                out = ie({'doc': img})
                print('  ✓ 成功')
                print(json.dumps(out, ensure_ascii=False)[:1200])
                results['A_自OCR'] = out
            except Exception as e:
                print(f'  ✗ {type(e).__name__}: {str(e)[:220]}')
                results['A_自OCR'] = {'error': f'{type(e).__name__}: {str(e)[:300]}'}
        else:
            results['A_自OCR'] = {'error': 'patch 失败'}

    if args.mode in ('b', 'both'):
        print()
        print('=' * 64)
        print('方案B：传自己的 layout，跳过内部 OCR')
        print('=' * 64)
        layout = load_artifact_layout(args.artifacts, img_name)
        if layout is None:
            print(f'  !! artifacts 里找不到 {img_name} 的记录')
            print('     需要先把 artifacts 目录放到当前工作目录')
        else:
            print(f'  从 artifacts 构造 layout，共 {len(layout)} 个 block')
            for b, t in layout[:3]:
                print(f'    bbox={b} text={t[:60]!r}')
            try:
                ie = build_ie('检验项目')
                out = ie({'doc': img, 'layout': layout})
                print('  ✓ 成功')
                print(json.dumps(out, ensure_ascii=False)[:1200])
                results['B_用layout'] = out
            except Exception as e:
                print(f'  ✗ {type(e).__name__}: {str(e)[:220]}')
                results['B_用layout'] = {'error': f'{type(e).__name__}: {str(e)[:300]}'}

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f'\n结果已保存: {args.out}')

    print()
    print('=' * 64)
    print('判读')
    print('=' * 64)
    ok = [k for k, v in results.items() if isinstance(v, list)]
    if ok:
        print(f'  ✓ 可用方案: {ok}')
        print('  重点看 schema="检验项目" 召回了几个不同项目名：')
        for k in ok:
            arr = results[k]
            if arr and isinstance(arr, list) and arr[0]:
                names = list(arr[0].keys()) if isinstance(arr[0], dict) else []
                print(f'    {k}: 返回字段 {names}')
                for fld in names:
                    spans = arr[0][fld]
                    print(f'      {fld}: {len(spans)} 个 span')
    else:
        print('  ✗ 两条都失败')


if __name__ == '__main__':
    main()
