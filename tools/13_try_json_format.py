# -*- coding: utf-8 -*-
"""13 云端：尝试用 paddle 3.x 的 json 格式救活 UIE-X（不换 paddle）

关键发现（2026-10-27）：
    static 目录里实际已生成：
        inference.json       0.7 MB    ← paddle 3.x 的计算图结构
        inference.pdiparams  1072 MB   ← 权重
    但缺 inference.pdmodel（paddle 2.x 的结构文件名）。
    paddlenlp 2.8 硬编码读 .pdmodel，所以报 NotFound。

用法：
    python tools/13_try_json_format.py"""

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


def show():
    print('=' * 64)
    print('0. 现状')
    print('=' * 64)
    for p in (JSON_MODEL, PARAMS, PDMODEL):
        ok = os.path.isfile(p)
        sz = f'{os.path.getsize(p)/1024/1024:.1f}MB' if ok else '-'
        print(f'  {os.path.basename(p):<22} {"✓" if ok else "✗"} {sz}')
    if not os.path.isfile(JSON_MODEL):
        print('\n  !! inference.json 不存在，本方案不适用')
        return False
    return True


def test_config_with_json():
    """直接测 paddle.inference.Config 能否吃 json。"""
    print()
    print('=' * 64)
    print('① 测试 paddle.inference.Config 加载 json')
    print('=' * 64)
    import paddle
    print('  paddle:', paddle.__version__)
    try:
        cfg = paddle.inference.Config(JSON_MODEL, PARAMS)
        print('  ✓ Config 接受 json 路径')
    except Exception as e:
        print(f'  ✗ Config 拒绝 json: {type(e).__name__}: {str(e)[:160]}')
        return False
    try:
        predictor = paddle.inference.create_predictor(cfg)
        names = predictor.get_input_names()
        print('  ✓ create_predictor 成功')
        print('    输入名:', names[:8])
        print('    输出名:', predictor.get_output_names()[:8])
        return True
    except Exception as e:
        print(f'  ✗ create_predictor 失败: {type(e).__name__}: {str(e)[:200]}')
        import traceback
        traceback.print_exc()
        return False


def test_copy_as_pdmodel():
    """把 json 复制成 pdmodel 名字，看 paddle 是否按内容识别。"""
    print()
    print('=' * 64)
    print('② 把 json 复制为 inference.pdmodel 后重试')
    print('=' * 64)
    if os.path.isfile(PDMODEL):
        print('  pdmodel 已存在，跳过复制')
    else:
        shutil.copyfile(JSON_MODEL, PDMODEL)
        print(f'  已复制 {os.path.basename(JSON_MODEL)} -> inference.pdmodel')

    import paddle
    try:
        cfg = paddle.inference.Config(PDMODEL, PARAMS)
        predictor = paddle.inference.create_predictor(cfg)
        print('  ✓ 用 pdmodel 名字加载 json 内容，成功')
        print('    输入名:', predictor.get_input_names()[:6])
        return True
    except Exception as e:
        print(f'  ✗ 失败: {type(e).__name__}: {str(e)[:200]}')
        # 清理失败的假 pdmodel，避免后续一直报错
        try:
            os.remove(PDMODEL)
            print('  已删除无效的 pdmodel 副本')
        except OSError:
            pass
        return False


def try_taskflow():
    print()
    print('=' * 64)
    print('③ 若前面成功，尝试 Taskflow 端到端')
    print('=' * 64)
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
        print(f'  ✗ {type(e).__name__}: {str(e)[:220]}')
        return None


def main():
    if not show():
        return

    ok_json = test_config_with_json()

    if ok_json:
        print('\n结论: Config 已支持 json。需要 monkey patch paddlenlp 让它读 json 而非 pdmodel。')
        print('  下一步我会据此改 08 脚本的加载路径。')
        ie = try_taskflow()
        if ie:
            print('  （Taskflow 竟然直接通过了，说明 paddlenlp 内部有回退逻辑）')
        return

    ok_copy = test_copy_as_pdmodel()
    if ok_copy:
        ie = try_taskflow()
        if ie:
            print('\n结论: 复制成 pdmodel 名字即可用！应把该操作固化进脚本。')
        else:
            print('\n结论: Config 层可用，但 paddlenlp 内部还有别的不兼容点。')
        return

    print()
    print('=' * 64)
    print('结论')
    print('=' * 64)
    print('  ✗ json 格式也无法被当前 paddle 的推理API直接消费。')
    print('  说明 paddle 3.3 的 json 与 2.x 的 pdmodel 是两套不互通的格式。')
    print()
    print('  此时不建议换 paddle（会破坏 paddleocr 3.7，得不偿失）。')
    print('  建议转方案 B：PaddleOCR-VL 直接输出结构化 JSON。')


if __name__ == '__main__':
    main()
