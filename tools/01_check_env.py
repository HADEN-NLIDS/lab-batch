# -*- coding: utf-8 -*-
"""环境检查：paddle、设备与 PaddleOCR 是否可用

用法：python tools/01_check_env.py
      AI Studio Notebook 里可粘贴运行。"""

import os
import sys
import time
import platform

print("=" * 60)
print("1. 基础环境")
print("=" * 60)
print("Python :", sys.version.split()[0])
print("平台   :", platform.platform())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_env import DEVICE, MODEL_SOURCE  # noqa: E402

print("DEVICE       =", DEVICE)
print("MODEL SOURCE =", MODEL_SOURCE)

print()
print("=" * 60)
print("2. Paddle 与设备")
print("=" * 60)
try:
    import paddle
except ImportError:
    print("!! paddle 未安装")
    sys.exit(1)

print("paddle 版本   :", paddle.__version__)
try:
    devices = paddle.device.get_all_custom_device_type()
except Exception as e:
    devices = f"查询失败: {e}"
print("自定义设备类型:", devices)

try:
    paddle.set_device(DEVICE)
    print("set_device    :", DEVICE, "成功")
except Exception as e:
    print("!! set_device 失败:", e)
    sys.exit(1)

# 小算子连通性测试
try:
    x = paddle.randn([4, 4])
    y = paddle.matmul(x, x)
    paddle.device.synchronize()
    print("小算子测试    : matmul 成功, 结果形状", y.shape)
except Exception as e:
    print("!! 算子测试失败:", e)
    sys.exit(1)

print()
print("=" * 60)
print("3. PaddleOCR 是否可用")
print("=" * 60)
try:
    import paddleocr

    print("paddleocr 版本:", getattr(paddleocr, "__version__", "unknown"))
    HAS_OCR = True
except ImportError as e:
    print("!! paddleocr 未安装:", e)
    print("   安装命令（Notebook 里直接跑，不加 !）:")
    print('   pip install -U "paddleocr[doc-parser]"')
    HAS_OCR = False

print()
print("=" * 60)
print("4. 结果汇总")
print("=" * 60)
print(
    f"Python {sys.version.split()[0]} | paddle {paddle.__version__} | "
    f'device {DEVICE} | paddleocr {"有" if HAS_OCR else "无"}'
)
print()
print("环境检查通过后，下一步是跑真实OCR验证（见 ocr_smoke_test.py）")
