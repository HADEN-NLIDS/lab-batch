# -*- coding: utf-8 -*-
"""15 云端：只走 monkey patch —— 让 paddlenlp 真正拿到 .json 路径

背景（2026-10-27 已验证的三件事）：
    1. paddle.inference.Config 直接吃 .json 路径 → 成功，predictor 可创建
       输入: input_ids/token_type_ids/position_ids/attention_mask/bbox/image
    2. 把 json 内容复制成 .pdmodel 文件名 → **进程段错误退出（无traceback）**
       说明 paddle 按扩展名选解析器，新格式喂旧解析器会崩
    3. paddlenlp 调用点是 paddle.inference.Config(self._static_model_file, ...)
       即模块属性访问，因此 patch paddle.inference.Config 可以生效

用法：
    python tools/15_patch_config_only.py"""

import os
import sys
import importlib

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')

BASE = os.path.expanduser(
    '~/.paddlenlp/taskflow/information_extraction/uie-x-base/static')
JSON_MODEL = os.path.join(BASE, 'inference.json')
PDMODEL = os.path.join(BASE, 'inference.pdmodel')


def cleanup_fake_pdmodel():
    """删掉上一轮复制出来的假 pdmodel（它会让进程段错误）。"""
    if os.path.isfile(PDMODEL):
        try:
            os.remove(PDMODEL)
            print('  已删除上一轮的假 inference.pdmodel（它会导致段错误）')
        except OSError as e:
            print(f'  !! 删除失败: {e}')
            print('  请手动执行: rm', PDMODEL)
            sys.exit(1)
    else:
        print('  无残留 pdmodel')


def install_shim():
    try:
        hub = importlib.import_module('aistudio_sdk.hub')
        if not hasattr(hub, 'download'):
            hub.download = lambda *a, **k: None
    except Exception:
        pass


def patch_config():
    import paddle

    if getattr(paddle.inference, '_uiex_patched', False):
        print('  Config 已打过补丁')
        return

    _orig = paddle.inference.Config

    def _patched(model_file, params_file=None):
        if isinstance(model_file, str) and model_file.endswith('.pdmodel'):
            cand = model_file[:-len('.pdmodel')] + '.json'
            if os.path.isfile(cand):
                print(f'  [patch] {os.path.basename(model_file)}'
                      f' -> {os.path.basename(cand)}')
                model_file = cand
        return _orig(model_file, params_file)

    paddle.inference.Config = _patched
    paddle.inference._uiex_patched = True
    print('  已替换 paddle.inference.Config')


def main():
    print('=' * 64)
    print('1. 清理残留')
    print('=' * 64)
    cleanup_fake_pdmodel()
    if not os.path.isfile(JSON_MODEL):
        print('  !! inference.json 不存在，无法继续')
        sys.exit(1)
    print(f'  inference.json: {os.path.getsize(JSON_MODEL)/1024/1024:.1f} MB')

    print()
    print('=' * 64)
    print('2. 打补丁')
    print('=' * 64)
    install_shim()
    patch_config()

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
        print('\n  ✓✓ Taskflow 初始化成功！UIE-X 在 paddle 3.3 上跑通了')
        print()
        print('  下一步: 把下面这段补丁加进 08_uiex_smoke.py 后再跑冒烟')
        print()
        print('    ' + '-' * 56)
        print('    import paddle, os')
        print('    _orig = paddle.inference.Config')
        print('    def _patched(mf, pf=None):')
        print('        if isinstance(mf, str) and mf.endswith(".pdmodel"):')
        print('            c = mf[:-len(".pdmodel")] + ".json"')
        print('            if os.path.isfile(c): mf = c')
        print('        return _orig(mf, pf)')
        print('    paddle.inference.Config = _patched')
        print('    ' + '-' * 56)
    except Exception as e:
        print(f'\n  ✗ {type(e).__name__}: {str(e)[:300]}')
        import traceback
        traceback.print_exc()
        print()
        print('  若报错仍是 pdmodel NotFound → patch 没作用到调用点，')
        print('    需查 paddlenlp/taskflow/task.py:371 附近源码')
        print('  若报错变成别的 → 新问题，按 traceback 定位')


if __name__ == '__main__':
    main()
