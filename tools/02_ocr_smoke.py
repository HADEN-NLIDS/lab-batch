# -*- coding: utf-8 -*-
"""PaddleOCR-VL 在天数 Iluvatar GPU 上的最小冒烟测试

目标：用一张图跑通 "加载模型 → 推理 → 拿到结构化结果"，并记录耗时与显存。
这是整个批量方案能否成立的前置验证。

用法：在 AI Studio Notebook 中新建 cell，粘贴运行；
      或上传本文件后 !python ocr_smoke_test.py"""

import os
import sys
import time
import glob

# --- 环境变量必须在 import paddle 之前设置 ---
os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
# AI Studio 内置模型源，避开 HuggingFace 网络问题。
# 可选值：aistudio / bos / modelscope / huggingface
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']
print('DEVICE =', DEVICE)
print('MODEL SOURCE =', os.environ['PADDLE_PDX_MODEL_SOURCE'])

import paddle  # noqa: E402
paddle.set_device(DEVICE)


# ---------------------------------------------------------------
# 1. 准备测试图片
# ---------------------------------------------------------------
CANDIDATES = ['test_report.jpg', 'test_report.png', 'test_report.jpeg']
IMG = next((f for f in CANDIDATES if os.path.exists(f)), None)

if IMG is None:
    # 工作目录下已有的图片里挑一张
    imgs = [f for f in glob.glob('*.jpg') + glob.glob('*.png')
            if not f.startswith('.')]
    if imgs:
        IMG = imgs[0]
        print('未找到 test_report.*，使用已有图片:', IMG)

if IMG is None:
    print('未找到图片，生成一张模拟化验单...')
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        os.system('pip install -q pillow')
        from PIL import Image, ImageDraw

    W, H = 1240, 1754  # A4 @200dpi
    img = Image.new('RGB', (W, H), 'white')
    d = ImageDraw.Draw(img)
    d.text((60, 50), 'LABORATORY REPORT', fill='black')
    d.text((60, 90), 'Name: ZHANG SAN    Age: 35    Date: 2026-10-27', fill='black')
    # 表格线
    x0, y0, cw, rh = 60, 160, 280, 50
    headers = ['Item', 'Result', 'Unit', 'Reference']
    for i, h in enumerate(headers):
        d.text((x0 + i * cw + 10, y0 + 15), h, fill='black')
    rows = [
        ['WBC', '6.2', '10^9/L', '3.5-9.5'],
        ['RBC', '4.85', '10^12/L', '4.3-5.8'],
        ['HGB', '152', 'g/L', '130-175'],
        ['ALT', '68', 'U/L', '9-50'],
        ['GLU', '6.4', 'mmol/L', '3.9-6.1'],
    ]
    for r, row in enumerate(rows, start=1):
        for i, val in enumerate(row):
            d.text((x0 + i * cw + 10, y0 + r * rh + 15), val, fill='black')
        d.line([(x0, y0 + r * rh), (x0 + 4 * cw, y0 + r * rh)], fill='gray')
    for i in range(5):
        d.line([(x0, y0), (x0, y0 + 6 * rh)], fill='gray')
        d.line([(x0 + i * cw, y0), (x0 + i * cw, y0 + 6 * rh)], fill='gray')
    IMG = 'test_report_synthetic.png'
    img.save(IMG)
    print('已生成:', IMG)

print('测试图片:', IMG, os.path.getsize(IMG), 'bytes')


# ---------------------------------------------------------------
# 2. 加载 OCR 产线
# ---------------------------------------------------------------
print()
print('=' * 60)
print('加载 PaddleOCR-VL ...')
print('=' * 60)
try:
    from paddleocr import PaddleOCRVL
except ImportError:
    print('!! paddleocr 未安装，请先执行（Notebook 里不加 !）:')
    print('   pip install -U "paddleocr[doc-parser]"')
    sys.exit(1)

t0 = time.time()
try:
    pipeline = PaddleOCRVL(device=DEVICE)
except Exception as e:
    print('!! 产线初始化失败:', type(e).__name__, e)
    print()
    print('排查建议：')
    print('1) 模型下载失败 → 换源: os.environ["PADDLE_PDX_MODEL_SOURCE"]="bos" 或 "modelscope"')
    print('2) 显存不足     → 减小 batch 或改用 PP-StructureV3 降级')
    print('3) 算子不支持   → 记录完整 traceback 反馈')
    sys.exit(1)
load_s = time.time() - t0
print(f'初始化完成，耗时 {load_s:.1f} 秒')


# ---------------------------------------------------------------
# 3. 推理
# ---------------------------------------------------------------
print()
print('=' * 60)
print('开始推理（首次可能较慢，请耐心）')
print('=' * 60)
t1 = time.time()
try:
    output = pipeline.predict(IMG)
    # 取出结果（predict 返回可迭代对象）
    results = list(output)
except Exception as e:
    print('!! 推理失败:', type(e).__name__, e)
    import traceback
    traceback.print_exc()
    sys.exit(1)
infer_s = time.time() - t1
print(f'推理完成，耗时 {infer_s:.1f} 秒，共 {len(results)} 个结果对象')


# ---------------------------------------------------------------
# 4. 查看结果结构
# ---------------------------------------------------------------
print()
print('=' * 60)
print('结果结构（前 1 个对象）')
print('=' * 60)
if results:
    r = results[0]
    print('类型:', type(r).__name__)
    for attr in ('json', 'markdown', 'parsing_res_list'):
        if hasattr(r, attr):
            val = getattr(r, attr)
            preview = str(val)
            print(f'  .{attr} = {preview[:300]}')
else:
    print('结果为空')


# ---------------------------------------------------------------
# 5. 显存与结论
# ---------------------------------------------------------------
print()
print('=' * 60)
print('汇总')
print('=' * 60)
try:
    alloc = paddle.device.memory_allocated(DEVICE) / 1024 ** 3
    peak = paddle.device.max_memory_allocated(DEVICE) / 1024 ** 3
    print(f'显存占用: 当前 {alloc:.2f} GB / 峰值 {peak:.2f} GB')
except Exception as e:
    print('显存查询不可用:', e)

print(f'加载 {load_s:.1f}s + 推理 {infer_s:.1f}s = 总计 {load_s + infer_s:.1f}s')
print()
print('结论：')
print('  若已拿到非空结果 → OCR链路在天数卡上可用，方案成立')
print('  记录本次 [加载/推理] 耗时，作为性能基线的第一组数据')
print('  注意：课程版"3-5分钟/页"是RTX 4060数据，不可跨平台套用')
