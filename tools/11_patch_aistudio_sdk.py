# -*- coding: utf-8 -*-
"""11 云端：绕过 aistudio_sdk 接口不兼容，让 paddlenlp 能导入

根因（2026-10-27 诊断）：
    paddlenlp/transformers/aistudio_utils.py:17
        from aistudio_sdk.hub import download   ← ImportError
    环境里的 aistudio_sdk 版本没有 download 函数，导致 paddlenlp 整个包导入失败。
    这跟 paddle 版本无关，是 AI Studio SDK 接口变更。

用法：
    python tools/11_patch_aistudio_sdk.py
    成功后会输出可复用的 shim 代码片段。"""

import sys
import importlib

sys.stdout.reconfigure(encoding='utf-8')


def inspect_sdk():
    print('=' * 64)
    print('1. aistudio_sdk 现状')
    print('=' * 64)
    try:
        sdk = importlib.import_module('aistudio_sdk')
        print('  包路径:', getattr(sdk, '__file__', '?'))
        try:
            import importlib.metadata as md
            print('  版本  :', md.version('aistudio_sdk'))
        except Exception:
            print('  版本  : (未知)')
    except Exception as e:
        print('  ✗ 无法导入 aistudio_sdk:', e)
        return None

    try:
        hub = importlib.import_module('aistudio_sdk.hub')
    except Exception as e:
        print('  ✗ 无法导入 aistudio_sdk.hub:', e)
        return None

    attrs = [a for a in dir(hub) if not a.startswith('_')]
    print('  hub 模块可用属性:', attrs)
    print('  是否有 download:', hasattr(hub, 'download'))
    return hub


def make_shim(hub):
    """给 hub 补一个 download。优先复用 SDK 内语义相近的函数。"""
    if hub is None or hasattr(hub, 'download'):
        return
    candidates = ['download_from_hub', 'download_dataset', 'download_file',
                  'get_model', 'download_model']
    for name in candidates:
        fn = getattr(hub, name, None)
        if callable(fn):
            setattr(hub, 'download', fn)
            print(f'  ✓ 用 aistudio_sdk.hub.{name} 作为 download 的替代')
            return
    # 都找不到就放一个惰性垫片：只在真的被调用时才报错
    def _lazy_download(*args, **kwargs):
        raise RuntimeError(
            'aistudio_sdk.hub.download 不可用（垫片）。'
            '若触发到这里，说明 paddlenlp 正在尝试从 AI Studio 下载模型，'
            '请改为手动下载模型或设置 PADDLE_PDX_MODEL_SOURCE。')
    setattr(hub, 'download', _lazy_download)
    print('  ✓ 已注入惰性 download 垫片（仅在真正被调用时抛错）')


def try_import_paddlenlp():
    print()
    print('=' * 64)
    print('3. 打补丁后重新导入 paddlenlp')
    print('=' * 64)
    for mod in list(sys.modules):
        if mod.startswith('paddlenlp'):
            del sys.modules[mod]
    try:
        import paddlenlp
        print('  ✓ import paddlenlp 成功')
        print('    版本:', getattr(paddlenlp, '__version__', '?'))
    except Exception as e:
        print(f'  ✗ 仍然失败: {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()
        return False

    try:
        from paddlenlp import Taskflow
        print('  ✓ from paddlenlp import Taskflow 成功')
        return True
    except Exception as e:
        print(f'  ✗ Taskflow 导入失败: {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()
        return False


def main():
    hub = inspect_sdk()
    print()
    print('=' * 64)
    print('2. 注入垫片')
    print('=' * 64)
    if hub is None:
        print('  !! SDK 不可用，无法打补丁')
        return
    make_shim(hub)

    ok = try_import_paddlenlp()

    print()
    print('=' * 64)
    print('4. 结论')
    print('=' * 64)
    if ok:
        print('  ✓ 可用！在其它脚本里 import paddlenlp 之前，先执行同样三步：')
        print()
        print('      import importlib')
        print('      hub = importlib.import_module("aistudio_sdk.hub")')
        print('      if not hasattr(hub, "download"):')
        print('          hub.download = lambda *a, **k: None')
        print('      from paddlenlp import Taskflow')
        print()
        print('  下一步: python tools/08_uiex_smoke.py --images images --limit 1')
    else:
        print('  ✗ 垫片未能解决。可能还有第二处不兼容，看上面 traceback 决定。')
        print('  备选方案：PaddleOCR-VL 直接输出结构化JSON（零新依赖）')


if __name__ == '__main__':
    main()
