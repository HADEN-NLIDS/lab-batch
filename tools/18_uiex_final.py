# -*- coding: utf-8 -*-
"""18 云端：修好 layout 污染 + 正确的 PaddleOCR patch（UIE-X 最后一轮）

上一轮的两个问题：
    方案A：patch 找错位置。doc_parser 是函数内 `from paddleocr import PaddleOCR`，
           所以要 patch paddleocr.PaddleOCR 模块属性本身
    方案B：返回 [{}]，layout 文本里混进了 `#################` 分隔符，且有空 block

用法：
    python tools/18_uiex_final.py --mode both"""

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
SEP = '#################'


def install_base_patches():
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


def patch_paddleocr_class():
    """方案A：patch paddleocr.PaddleOCR 类本身，吞掉 show_log。"""
    try:
        import paddleocr
    except Exception as e:
        print('  !! 无法导入 paddleocr:', e)
        return False
    if getattr(paddleocr, '_uiex_shimmed', False):
        return True
    _Orig = paddleocr.PaddleOCR

    class _Shim(_Orig):
        def __init__(self, *a, **kw):
            kw.pop('show_log', None)
            kw.pop('use_gpu', None)      # 3.x 也是废弃参数，一并吞掉
            kw.pop('use_onnx', None)
            super().__init__(*a, **kw)

    paddleocr.PaddleOCR = _Shim
    paddleocr._uiex_shimmed = True
    # 有些代码已 from ... import 绑定，需要再覆盖已加载模块中的引用
    for modname in ('paddlenlp.utils.doc_parser',):
        try:
            m = importlib.import_module(modname)
            if hasattr(m, 'PaddleOCR'):
                m.PaddleOCR = _Shim
        except Exception:
            pass
    print('  [patch4] 已替换 paddleocr.PaddleOCR（吞掉 show_log 等废弃参数）')
    return True


def clean_layout_text(raw):
    """把 HTML 表格转成模型可读文本，去掉分隔符残留。"""
    t = raw.split(SEP)[0]
    t = re.sub(r'</t[dh]>', ' | ', t, flags=re.I)
    t = re.sub(r'</tr>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    t = (t.replace('&nbsp;', ' ').replace('&lt;', '<')
          .replace('&gt;', '>').replace('&amp;', '&'))
    t = re.sub(r'[ \t]+', ' ', t)
    lines = [ln.strip() for ln in t.split('\n') if ln.strip()]
    return '\n'.join(lines)


def load_artifact_layout(artifacts_dir, image_name):
    for f in glob.glob(os.path.join(artifacts_dir, '*.json')):
        if os.path.basename(f).startswith('_'):
            continue
        rec = json.load(open(f, encoding='utf-8'))
        if rec.get('image') != image_name:
            continue
        layout = []
        for res in (rec.get('results') or []):
            prl = res.get('parsing_res_list') or []
            # 一个字符串里可能含多个 block，按分隔符切开
            for s in prl:
                if not isinstance(s, str):
                    continue
                for part in s.split(SEP):
                    if 'label:' not in part:
                        continue
                    m_box = re.search(r'bbox:\s*\[([^\]]*)\]', part)
                    m_con = re.search(r'content:\s*(.*)', part, re.S)
                    if not m_box:
                        continue
                    try:
                        box = [float(x.strip()) for x in m_box.group(1).split(',')]
                    except ValueError:
                        continue
                    if len(box) != 4:
                        continue
                    text = clean_layout_text(m_con.group(1).strip() if m_con else '')
                    if text and len(text) >= 2:
                        layout.append((box, text))
        return layout
    return None


def build_ie(schema):
    from paddlenlp import Taskflow
    return Taskflow('information_extraction', schema=schema,
                    model='uie-x-base', ocr_lang='ch', schema_lang='ch')


def run_one(tag, ie, payload):
    try:
        out = ie(payload)
        n_fields = len(out[0]) if out and isinstance(out[0], dict) else 0
        n_spans = 0
        if out and isinstance(out[0], dict):
            for k, v in out[0].items():
                if isinstance(v, list):
                    n_spans += len(v)
        print(f'  ✓ {tag}: 返回字段{n_fields}个, span{n_spans}个')
        if n_fields:
            print('    ', json.dumps(out[0], ensure_ascii=False)[:800])
        else:
            print('    （空对象 {}，一个字段都没抽到）')
        return out
    except Exception as e:
        print(f'  ✗ {tag}: {type(e).__name__}: {str(e)[:200]}')
        return {'error': f'{type(e).__name__}: {str(e)[:300]}'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mode', default='both', choices=['a', 'b', 'both'])
    ap.add_argument('--images', default='images')
    ap.add_argument('--artifacts', default='artifacts')
    ap.add_argument('--out', default='uiex_final.json')
    args = ap.parse_args()

    install_base_patches()
    print('基础补丁已装')

    imgs = []
    for ext in ('*.jpg', '*.jpeg', '*.png'):
        imgs.extend(sorted(glob.glob(os.path.join(args.images, ext))))
    if not imgs:
        print(f'!! {args.images} 下没有图片')
        sys.exit(1)
    img = imgs[0]
    img_name = os.path.basename(img)
    print('测试图:', img)

    results = {}

    if args.mode in ('a', 'both'):
        print()
        print('=' * 64)
        print('方案A：patch paddleocr.PaddleOCR，让 UIE-X 自己 OCR')
        print('=' * 64)
        if patch_paddleocr_class():
            try:
                ie = build_ie('检验项目')
                results['A_自OCR'] = run_one('A', ie, {'doc': img})
            except Exception as e:
                print(f'  ✗ 初始化失败: {type(e).__name__}: {str(e)[:200]}')
                results['A_自OCR'] = {'error': 'init failed'}

    if args.mode in ('b', 'both'):
        print()
        print('=' * 64)
        print('方案B：传清理过的 layout')
        print('=' * 64)
        layout = load_artifact_layout(args.artifacts, img_name)
        if not layout:
            print('  !! artifacts 里没有该图的记录或 layout 为空')
        else:
            print(f'  layout block 数: {len(layout)}')
            for b, t in layout[:4]:
                print(f'    bbox={b} text={t[:70]!r}')
            try:
                ie = build_ie('检验项目')
                results['B_用layout'] = run_one('B', ie, {'doc': img, 'layout': layout})
            except Exception as e:
                print(f'  ✗ 初始化失败: {type(e).__name__}: {str(e)[:200]}')
                results['B_用layout'] = {'error': 'init failed'}

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f'\n结果: {args.out}')

    print()
    print('=' * 64)
    print('判读')
    print('=' * 64)
    good = {k: v for k, v in results.items()
            if isinstance(v, list) and v and isinstance(v[0], dict) and v[0]}
    if good:
        for k, v in good.items():
            print(f'  ✓ {k} 抽到了内容')
            for fld, spans in v[0].items():
                print(f'      {fld}: {len(spans)} 个 span')
                for sp in spans[:5]:
                    print(f'        - {sp.get("text","")[:40]!r} p={sp.get("probability",0):.2f}')
    else:
        print('  ✗ 没有任何方案抽到内容。')
        print()
        print('  说明 UIE-X 这条路在当前环境/数据上不可行，')
        print('  应转 PaddleOCR-VL 直接输出结构化 JSON。')


if __name__ == '__main__':
    main()
