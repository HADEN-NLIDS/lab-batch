# -*- coding: utf-8 -*-
"""12 云端：绕过 UIE-X 静态图转换失败（PIR 不兼容）

根因（2026-10-27）：
    模型下载成功、权重加载成功，但 Taskflow 要把动态图转成静态推理图时失败：
        Paddle static graph(PIR) not support input out tensor for now!!!!!
        RuntimeError: Cannot open file .../static/inference.pdmodel
    paddle 3.x 引入的 PIR 中间表示与 paddlenlp 2.8 的动转静逻辑不兼容，
    转换没产出 .pdmodel 文件，后续加载自然失败。

用法：
    python tools/12_try_disable_pir.py"""

import os
import sys
import glob
import importlib
import subprocess

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')

MODEL_DIR = os.path.expanduser(
    '~/.paddlenlp/taskflow/information_extraction/uie-x-base')
STATIC_DIR = os.path.join(MODEL_DIR, 'static')


def install_shim():
    """aistudio_sdk.hub.download 垫片，缺了它 paddlenlp 根本导入不了。"""
    try:
        hub = importlib.import_module('aistudio_sdk.hub')
        if not hasattr(hub, 'download'):
            hub.download = lambda *a, **k: None
    except Exception:
        pass


def show_static_dir():
    print('=' * 64)
    print('1. static 目录实际内容')
    print('=' * 64)
    print('  路径:', STATIC_DIR)
    if not os.path.isdir(STATIC_DIR):
        print('  !! 目录不存在（说明转换在创建目录前就失败了）')
        return []
    files = []
    for root, _, fs in os.walk(STATIC_DIR):
        for f in fs:
            p = os.path.join(root, f)
            sz = os.path.getsize(p)
            files.append(p)
            print(f'  {os.path.relpath(p, STATIC_DIR):<40} {sz/1024/1024:.1f} MB')
    if not files:
        print('  !! 目录为空（转换启动但没产出文件）')
    # 关键文件
    for need in ('inference.pdmodel', 'inference.pdiparams'):
        hits = glob.glob(os.path.join(STATIC_DIR, '**', need), recursive=True)
        print(f'  需要 {need}: {"✓ " + hits[0] if hits else "✗ 缺失"}')
    return files


def try_taskflow(tag, flags=None):
    """在当前进程里尝试初始化 Taskflow。"""
    print()
    print('=' * 64)
    print(f'尝试: {tag}')
    print('=' * 64)
    import paddle
    if flags:
        try:
            paddle.set_flags(flags)
            print('  已设置 FLAGS:', flags)
        except Exception as e:
            print(f'  FLAGS 设置失败（忽略继续）: {e}')
    print('  当前 PIR 相关 FLAGS:')
    for k in ('FLAGS_enable_pir_api', 'FLAGS_enable_pir_in_executor'):
        try:
            print(f'    {k} =', paddle.get_flags([k])[0])
        except Exception:
            print(f'    {k} = (不可查询)')

    install_shim()
    for m in [m for m in list(sys.modules) if m.startswith('paddlenlp')]:
        del sys.modules[m]
    try:
        from paddlenlp import Taskflow
        ie = Taskflow('information_extraction', schema='检验项目',
                      model='uie-x-base', ocr_lang='ch', schema_lang='ch')
        print('  ✓ Taskflow 初始化成功！')
        return ie
    except Exception as e:
        print(f'  ✗ {type(e).__name__}: {str(e)[:200]}')
        return None


def main():
    files = show_static_dir()

    print()
    print('=' * 64)
    print('2. 逐种方式尝试')
    print('=' * 64)

    ie = None
    # ① 直接重试（模型已在缓存，若static已完整则可通过）
    ie = try_taskflow('直接重试（不改任何FLAGS）')
    if ie:
        print('\n结果: 直接重试即成功')
        return

    # ② 关闭 PIR API
    ie = try_taskflow('关闭 FLAGS_enable_pir_api',
                      {'FLAGS_enable_pir_api': False})
    if ie:
        print('\n结果: 关闭 PIR API 后成功')
        return

    # ③ 关闭执行器 PIR
    ie = try_taskflow('关闭 FLAGS_enable_pir_in_executor',
                      {'FLAGS_enable_pir_in_executor': False})
    if ie:
        print('\n结果: 关闭 PIR 执行器后成功')
        return

    print()
    print('=' * 64)
    print('3. 结论')
    print('=' * 64)
    print('  ✗ 三种方式均失败。paddle 3.3 的 PIR 与 paddlenlp 2.8 的动转静不兼容。')
    print()
    print('  可选路径（按推荐度）：')
    print('  A. 改用动态图直接调用 UIEX 模型，绕过 Taskflow 的动转静')
    print('     → 需自行处理 tokenizer/schema 编码，代码量中等')
    print('  B. 放弃 UIE-X，转 PaddleOCR-VL 直接输出结构化JSON（零新依赖）')
    print('  C. 降级 paddle 到 2.6（会破坏已跑通的 paddleocr 3.7，不推荐）')
    print()
    print('  建议先试 A；若 30 分钟内跑不通，直接转 B。')


if __name__ == '__main__':
    main()
