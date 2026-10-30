# -*- coding: utf-8 -*-
"""AI Studio (Iluvatar GPU) 环境与 OCR 最小验证

用法：在 AI Studio Notebook 中新建 cell，粘贴本文件内容直接运行，
      或上传本文件后 !python aistudio_env_check.py"""

import os
import sys
import time
import platform

print('=' * 60)
print('1. 基础环境')
print('=' * 60)
print('Python :', sys.version.split()[0])
print('平台   :', platform.platform())

# ---------------------------------------------------------------
# Notebook 里环境变量必须用 os.environ 设置，不能用 !export
# 原因：Jupyter 的每条 ! 命令都是独立 shell，export 不会传递给后续 cell
# ---------------------------------------------------------------
os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
print('PADDLE_XCCL_BACKEND =', os.environ['PADDLE_XCCL_BACKEND'])
print('PADDLE_DEVICE       =', os.environ['PADDLE_DEVICE'])

print()
print('=' * 60)
print('2. Paddle 与设备')
print('=' * 60)
try:
    import paddle
except ImportError:
    print('!! paddle 未安装')
    sys.exit(1)

print('paddle 版本   :', paddle.__version__)
try:
    devices = paddle.device.get_all_custom_device_type()
except Exception as e:
    devices = f'查询失败: {e}'
print('自定义设备类型:', devices)

DEVICE = os.environ['PADDLE_DEVICE']
try:
    paddle.set_device(DEVICE)
    print('set_device    :', DEVICE, '成功')
except Exception as e:
    print('!! set_device 失败:', e)
    sys.exit(1)

# 小算子连通性测试
try:
    x = paddle.randn([4, 4])
    y = paddle.matmul(x, x)
    paddle.device.synchronize()
    print('小算子测试    : matmul 成功, 结果形状', y.shape)
except Exception as e:
    print('!! 算子测试失败:', e)
    sys.exit(1)

print()
print('=' * 60)
print('3. PaddleOCR 是否可用')
print('=' * 60)
try:
    import paddleocr
    print('paddleocr 版本:', getattr(paddleocr, '__version__', 'unknown'))
    HAS_OCR = True
except ImportError as e:
    print('!! paddleocr 未安装:', e)
    print('   安装命令（Notebook 里直接跑，不加 !）:')
    print('   pip install -U "paddleocr[doc-parser]"')
    HAS_OCR = False

print()
print('=' * 60)
print('4. 结果汇总')
print('=' * 60)
print(f'Python {sys.version.split()[0]} | paddle {paddle.__version__} | '
      f'device {DEVICE} | paddleocr {"有" if HAS_OCR else "无"}')
print()
print('环境检查通过后，下一步是跑真实OCR验证（见 ocr_smoke_test.py）')
