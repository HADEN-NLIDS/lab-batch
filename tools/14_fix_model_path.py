# -*- coding: utf-8 -*-
"""14 云端：让 paddlenlp 读到 json 格式的推理模型，救活 UIE-X

背景（2026-10-27 已验证）：
    · static/ 里只有 inference.json(0.7MB) + inference.pdiparams(1072MB)
    · paddle.inference.Config 直接吃 json 成功，predictor 能创建
      输入: input_ids / token_type_ids / position_ids / attention_mask / bbox / image
    · 但 paddlenlp 硬编码读 inference.pdmodel，所以 NotFound

用法：
    python tools/14_fix_model_path.py"""

import os
import sys
import shutil
import importlib

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')

BASE = os.path.expanduser(
    '~/.paddlenlp/taskflow/information_extraction/uie-x-base/static')
JSON_MODEL = os.path.join(BASE, 'inference.json')
PARAMS = os.path.join(BASE, 'inference.pdiparams')
PDMODEL = os.path.join(BASE, 'inference.pdmodel')


def install_shim():
    try:
        hub = importlib.import_module('aistudio_sdk.hub')
        if not hasattr(hub, 'download'):
            hub.download = lambda *a, **k: None
    except Exception:
        pass


def reset_paddlenlp():
    for m in [m for m in list(sys.modules) if m.startswith('paddlenlp')]:
        del sys.modules[m]


def try_taskflow(tag):
    print()
    print('-' * 64)
    print(f'试 Taskflow: {tag}')
    print('-' * 64)
    install_shim()
    reset_paddlenlp()
    try:
        from paddlenlp import Taskflow
        ie = Taskflow('information_extraction', schema='检验项目',
                      model='uie-x-base', ocr_lang='ch', schema_lang='ch')
        print('  ✓ Taskflow 初始化成功！')
        return ie
    except Exception as e:
        msg = str(e).replace('\n', ' ')[:200]
        print(f'  ✗ {type(e).__name__}: {msg}')
        return None


def method1_copy():
    """① 把 json 复制成 pdmodel 文件名"""
    print('=' * 64)
    print('① 复制 inference.json -> inference.pdmodel')
    print('=' * 64)
    if not os.path.isfile(JSON_MODEL):
        print('  !! inference.json 不存在')
        return False
    if os.path.isfile(PDMODEL):
        print('  已存在，跳过复制')
    else:
        shutil.copyfile(JSON_MODEL, PDMODEL)
        print('  已复制')
    ie = try_taskflow('复制成 pdmodel 后')
    if ie:
        print('\n  ✓✓ 方案①成功：只要把 json 复制成 pdmodel 文件名即可')
        return True
    # 失败则删掉假 pdmodel，避免污染后续
    try:
        os.remove(PDMODEL)
        print('  已删除无效的 pdmodel 副本')
    except OSError:
        pass
    return False


def method2_patch():
    """② monkey patch paddle.inference.Config，把 .pdmodel 换成 .json"""
    print()
    print('=' * 64)
    print('② monkey patch paddle.inference.Config')
    print('=' * 64)
    import paddle

    if getattr(paddle.inference, '_uiex_patched', False):
        print('  已打过补丁')
    else:
        _orig = paddle.inference.Config

        def _patched(model_file, params_file=None):
            if isinstance(model_file, str) and model_file.endswith('.pdmodel'):
                cand = model_file[:-len('.pdmodel')] + '.json'
                if os.path.isfile(cand):
                    print(f'  [patch] {os.path.basename(model_file)} -> '
                          f'{os.path.basename(cand)}')
                    model_file = cand
            return _orig(model_file, params_file)

        paddle.inference.Config = _patched
        paddle.inference._uiex_patched = True
        print('  已替换 paddle.inference.Config')

    ie = try_taskflow('patch Config 路径后')
    if ie:
        print('\n  ✓✓ 方案②成功：需要 monkey patch Config 路径')
        return True
    return False


def main():
    print('=' * 64)
    print('目标：让 paddlenlp 读到 json 格式推理模型')
    print('=' * 64)
    print(f'  json    : {"✓" if os.path.isfile(JSON_MODEL) else "✗"}')
    print(f'  pdiparams: {"✓" if os.path.isfile(PARAMS) else "✗"}')

    if method1_copy():
        return
    if method2_patch():
        return

    print()
    print('=' * 64)
    print('结论')
    print('=' * 64)
    print('  ✗ 两种路径替换都失败。虽然 Config 层能吃 json，但 paddlenlp 走到的')
    print('    具体调用点还有别的差异（看上面的报错定位）。')
    print()
    print('  下一步选择：')
    print('  · 若报错仍是 pdmodel NotFound → 说明 patch 未作用到实际调用点，')
    print('    可继续查 paddlenlp/taskflow/task.py 的 _get_inference_model 实现')
    print('  · 若报错变成其它类型 → 新的问题，需具体看')


if __name__ == '__main__':
    main()
