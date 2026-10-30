# -*- coding: utf-8 -*-
"""16 云端：关掉 memory_optimize_pass，过最后一关

进展（2026-10-27）：
    · patch Config 路径后，报错从 "Cannot open file ...pdmodel"
      变成了 "Not find predictor_id 0 and pass_name memory_optimize_pass"
      → 说明路径已解决，现在是 config 的优化选项与 paddle 3.3 PIR 不兼容

用法：
    python tools/16_disable_memopt.py"""

import os
import sys
import importlib

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')

BASE = os.path.expanduser(
    '~/.paddlenlp/taskflow/information_extraction/uie-x-base/static')
JSON_MODEL = os.path.join(BASE, 'inference.json')
PDMODEL = os.path.join(BASE, 'inference.pdmodel')


def cleanup():
    if os.path.isfile(PDMODEL):
        os.remove(PDMODEL)
        print('  已清理残留 pdmodel')


def install_shim():
    try:
        hub = importlib.import_module('aistudio_sdk.hub')
        if not hasattr(hub, 'download'):
            hub.download = lambda *a, **k: None
    except Exception:
        pass


def patch_all():
    """同时打两个补丁：Config 路径 + create_predictor 前关优化。"""
    import paddle

    # --- 补丁1：把 .pdmodel 路径换成 .json ---
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
        print('  补丁1: Config 路径替换已装')

    # --- 补丁2：create_predictor 前关掉优化 ---
    if not getattr(paddle.inference, '_uiex_memopt_patched', False):
        _orig_create = paddle.inference.create_predictor

        def _patched_create(config):
            # 依次尝试关闭内存优化 / IR优化，哪个 API 存在用哪个
            attempts = [
                ('disable_memory_optim', ()),
                ('enable_memory_optim', (False,)),
                ('switch_ir_optim', (False,)),
            ]
            for name, args in attempts:
                fn = getattr(config, name, None)
                if not callable(fn):
                    continue
                try:
                    fn(*args)
                    print(f'  [patch2] config.{name}{args} 已调用')
                except TypeError:
                    continue
                except Exception as e:
                    print(f'  [patch2] config.{name} 失败: {type(e).__name__}: {e}')
            return _orig_create(config)

        paddle.inference.create_predictor = _patched_create
        paddle.inference._uiex_memopt_patched = True
        print('  补丁2: create_predictor 优化开关已装')


def main():
    print('=' * 64)
    print('1. 准备')
    print('=' * 64)
    cleanup()
    if not os.path.isfile(JSON_MODEL):
        print('  !! inference.json 不存在')
        sys.exit(1)
    install_shim()

    print()
    print('=' * 64)
    print('2. 打补丁')
    print('=' * 64)
    patch_all()

    print()
    print('=' * 64)
    print('3. 试 Taskflow')
    print('=' * 64)
    for m in [m for m in list(sys.modules) if m.startswith('paddlenlp')]:
        del sys.modules[m]
    try:
        from paddlenlp import Taskflow
        ie = Taskflow('information_extraction', schema='检验项目',
                      model='uie-x-base', ocr_lang='ch', schema_lang='ch')
        print('\n  ✓✓✓ Taskflow 初始化成功！UIE-X 在 paddle 3.3 上跑通了')
    except Exception as e:
        print(f'\n  ✗ {type(e).__name__}: {str(e)[:300]}')
        print()
        print('  若仍是 memory_optimize_pass → 换用 switch_ir_optim(False) 或查源码')
        print('  若变成别的错误 → 新问题')


if __name__ == '__main__':
    main()
