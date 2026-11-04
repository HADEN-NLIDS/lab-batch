# -*- coding: utf-8 -*-
"""识别环节的设备与模型源。

之前这几项固定在 AI Studio 的国产卡上，换到别的机器必须改代码。
现在按优先级取：显式环境变量 > 自动检测 CUDA > CPU。
"""

import os

MODEL_SOURCE = (os.environ.get("PADDLE_PDX_MODEL_SOURCE")
                or "modelscope").strip()
os.environ["PADDLE_PDX_MODEL_SOURCE"] = MODEL_SOURCE


def _detect_device():
    want = (os.environ.get("PADDLE_DEVICE") or "").strip()
    if want:
        return want
    try:
        import paddle
        if paddle.device.is_compiled_with_cuda() and \
                paddle.device.cuda.device_count() > 0:
            return "gpu:0"
    except Exception:
        pass
    return "cpu"


DEVICE = _detect_device()
os.environ.setdefault("PADDLE_DEVICE", DEVICE)

# XCCL 是国产卡的集合通信后端，NVIDIA 环境不需要
if "iluvatar" in DEVICE:
    os.environ.setdefault("PADDLE_XCCL_BACKEND", "iluvatar_gpu")
