# -*- coding: utf-8 -*-
"""08 云端：UIE-X 抽取冒烟测试（先验证能不能用、输出什么结构）

背景：UIE-X 原本是课程版的抽取层选型，但因 Python 3.13 依赖无解未验证。
      现在环境是 3.10，正是它可能可用的版本。

用法（AI Studio）：
    pip install paddlenlp
    python tools/08_uiex_smoke.py --images images --limit 1"""

import os
import sys
import json
import glob
import argparse

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
# PaddleNLP 模型源，避免走 huggingface
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--images', default='images', help='图片目录')
    ap.add_argument('--limit', type=int, default=1)
    ap.add_argument('--model', default='uie-x-base',
                    help='uie-x-base 或 uie-medical-base（后者为医疗版）')
    ap.add_argument('--out', default='uiex_smoke.json')
    return ap.parse_args()


def install_all_patches():
    """UIE-X 在此环境跑通需要三个补丁，缺一不可（2026-10-27 验证）。

    1. aistudio_sdk.hub.download 垫片
       否则 paddlenlp 导入就失败（其 aistudio_utils 会 from aistudio_sdk.hub import download）
    2. Config 路径替换 .pdmodel -> .json
       paddle 3.x 的动转静产物是 inference.json，paddlenlp 却硬编码读 .pdmodel
    3. create_predictor 前关闭 memory_optim / ir_optim
       paddle 3.3 的 PIR 模式不再执行 memory_optimize_pass，
       而 paddlenlp 仍去查其结果，报 "Not find predictor_id 0 and pass_name memory_optimize_pass" """
    import os
    import importlib

    # --- 补丁0：确保没有会导致段错误的假 pdmodel ---
    base = os.path.expanduser(
        '~/.paddlenlp/taskflow/information_extraction/uie-x-base/static')
    fake = os.path.join(base, 'inference.pdmodel')
    if os.path.isfile(fake):
        os.remove(fake)
        print('  [patch0] 已清理会导致段错误的假 inference.pdmodel')

    # --- 补丁1：aistudio_sdk 垫片 ---
    try:
        hub = importlib.import_module('aistudio_sdk.hub')
        if not hasattr(hub, 'download'):
            hub.download = lambda *a, **k: None
            print('  [patch1] 已注入 aistudio_sdk.hub.download 垫片')
    except Exception as e:
        print(f'  [patch1] 跳过: {type(e).__name__}')

    import paddle

    # --- 补丁2：Config 路径替换 ---
    if not getattr(paddle.inference, '_uiex_path_patched', False):
        _orig_cfg = paddle.inference.Config

        def _patched_cfg(model_file, params_file=None):
            if isinstance(model_file, str) and model_file.endswith('.pdmodel'):
                cand = model_file[:-len('.pdmodel')] + '.json'
                if os.path.isfile(cand):
                    model_file = cand
            return _orig_cfg(model_file, params_file)

        paddle.inference.Config = _patched_cfg
        paddle.inference._uiex_path_patched = True
        print('  [patch2] 已替换 Config 路径（.pdmodel -> .json）')

    # --- 补丁3：create_predictor 前关优化 ---
    if not getattr(paddle.inference, '_uiex_memopt_patched', False):
        _orig_create = paddle.inference.create_predictor

        def _patched_create(config):
            for name, args in (('disable_memory_optim', ()),
                               ('enable_memory_optim', (False,)),
                               ('switch_ir_optim', (False,))):
                fn = getattr(config, name, None)
                if callable(fn):
                    try:
                        fn(*args)
                    except Exception:
                        pass
            return _orig_create(config)

        paddle.inference.create_predictor = _patched_create
        paddle.inference._uiex_memopt_patched = True
        print('  [patch3] 已包装 create_predictor（关闭内存/IR优化）')


def main():
    args = parse_args()

    print('=' * 60)
    print('1. 依赖与补丁')
    print('=' * 60)
    try:
        import paddle
        print('paddle:', paddle.__version__)
    except ImportError:
        print('!! paddle 未安装')
        sys.exit(1)

    install_all_patches()

    try:
        import paddlenlp
        print('paddlenlp:', getattr(paddlenlp, '__version__', 'unknown'))
    except ImportError as e:
        print('!! paddlenlp 导入失败:', e)
        print('   先运行: python tools/11_patch_aistudio_sdk.py 排查')
        sys.exit(1)

    from paddlenlp import Taskflow

    # 图片
    imgs = []
    for ext in ('*.jpg', '*.jpeg', '*.png'):
        imgs.extend(sorted(glob.glob(os.path.join(args.images, ext))))
    if not imgs:
        print(f'!! {args.images} 下没有图片')
        sys.exit(1)
    targets = imgs[:args.limit]
    print(f'\n测试图片: {targets}')

    results = {}

    # 三种 schema 形态，看哪种适合表格多行
    schemas = {
        'A_平铺字段': ['检验项目', '结果', '单位', '参考范围'],
        'B_关系抽取': {'检验项目': ['结果', '单位', '参考范围']},
        'C_单字段探针': '检验项目',
    }

    for name, schema in schemas.items():
        print('\n' + '=' * 60)
        print(f'schema {name}: {schema}')
        print('=' * 60)
        try:
            ie = Taskflow('information_extraction', schema=schema,
                          model=args.model, ocr_lang='ch', schema_lang='ch')
            out = ie({'doc': targets[0]})
            print(json.dumps(out, ensure_ascii=False, indent=1)[:2500])
            results[name] = out
        except Exception as e:
            print(f'!! {name} 失败: {type(e).__name__}: {e}')
            import traceback
            traceback.print_exc()
            results[name] = {'error': f'{type(e).__name__}: {e}'}

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f'\n结果已保存: {args.out}')

    print('\n' + '=' * 60)
    print('判读要点')
    print('=' * 60)
    print('· 若 A 只返回单个值而非多行 → UIE-X 不适合表格循环抽取')
    print('· 若 B 能返回 {项目: [结果,单位,范围]} 的列表 → 可用关系抽取方案')
    print('· 若都只出零星结果 → 需要微调，或退回 PaddleOCR-VL 输出结构化JSON')


if __name__ == '__main__':
    main()
