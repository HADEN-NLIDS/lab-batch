# -*- coding: utf-8 -*-
"""PaddleOCR-VL 真实报告单批量性能测试（AI Studio / Iluvatar GPU）

针对 /home/aistudio/work/图片 目录设计，真实图与合成图分开统计。

用法：Notebook 中粘贴运行，或 !python ocr_batch_bench.py
     指定目录：BENCH_IMG_DIR=/your/path !python ocr_batch_bench.py"""

import os
import sys
import time
import glob
import shutil
import tempfile
import statistics

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']

import paddle  # noqa: E402
paddle.set_device(DEVICE)


# ---------------------------------------------------------------
# 1. 定位图片目录
# ---------------------------------------------------------------
CANDIDATES = [
    os.environ.get('BENCH_IMG_DIR'),
    '/home/aistudio/work/图片',
    '图片',
    'bench_images',
]
IMG_DIR = next((d for d in CANDIDATES if d and os.path.isdir(d)), None)

if IMG_DIR is None:
    print('未找到图片目录，请设置 BENCH_IMG_DIR 环境变量指向报告单所在目录')
    print('候选路径均不存在:', [c for c in CANDIDATES if c])
    sys.exit(1)

print(f'图片目录: {IMG_DIR}')

images = []
for ext in ('*.jpg', '*.jpeg', '*.png', '*.bmp', '*.tif', '*.tiff'):
    images.extend(glob.glob(os.path.join(IMG_DIR, ext)))
    images.extend(glob.glob(os.path.join(IMG_DIR, ext.upper())))
images = sorted(set(images))

if not images:
    print(f'!! 目录中未找到图片: {IMG_DIR}')
    sys.exit(1)


def is_synthetic(name):
    n = os.path.basename(name).lower()
    return 'synth' in n or 'test_report_synthetic' in n


real_imgs = [p for p in images if not is_synthetic(p)]
synth_imgs = [p for p in images if is_synthetic(p)]

print(f'共 {len(images)} 张：真实报告单 {len(real_imgs)} 张，合成图 {len(synth_imgs)} 张')
for p in images:
    tag = '合成' if is_synthetic(p) else '真实'
    size_kb = os.path.getsize(p) / 1024
    print(f'  [{tag}] {os.path.basename(p):<40} {size_kb:>8.1f} KB')


# ---------------------------------------------------------------
# 2. 处理中文路径（cv2.imread 不支持非 ASCII）
# ---------------------------------------------------------------
def ensure_ascii_paths(paths):
    if all(str(p).isascii() for p in paths):
        return paths, None
    tmpdir = tempfile.mkdtemp(prefix='bench_ascii_')
    mapping = []
    for i, p in enumerate(paths):
        ext = os.path.splitext(p)[1].lower() or '.png'
        dst = os.path.join(tmpdir, f'img_{i:03d}{ext}')
        shutil.copy2(p, dst)
        mapping.append((p, dst))
    print(f'\n检测到中文路径，已复制 {len(paths)} 张到英文临时目录: {tmpdir}')
    return [m[1] for m in mapping], tmpdir


all_imgs = real_imgs + synth_imgs
labels = [os.path.basename(p) for p in all_imgs]
work_imgs, tmpdir = ensure_ascii_paths(all_imgs)


# ---------------------------------------------------------------
# 3. 加载产线（仅一次）
# ---------------------------------------------------------------
from paddleocr import PaddleOCRVL  # noqa: E402

print('\n加载 PaddleOCR-VL ...')
t_load = time.time()
pipeline = PaddleOCRVL(device=DEVICE)
load_s = time.time() - t_load
print(f'加载完成: {load_s:.1f} 秒\n')


# ---------------------------------------------------------------
# 4. 逐张推理
# ---------------------------------------------------------------
results = []   # (label, is_synth, seconds, n_objs, ok)
print(f'{"#":>3} {"文件":<34} {"类别":<6} {"耗时(s)":>9} {"结果":>5}')
print('-' * 64)

for i, (wl, label) in enumerate(zip(work_imgs, labels), 1):
    synth = is_synthetic(label)
    t0 = time.time()
    try:
        out = list(pipeline.predict(wl))
        ok, n = True, len(out)
    except Exception as e:
        print(f'{i:>3} {label:<34} {"合成" if synth else "真实":<6} {"FAIL":>9} '
              f'{type(e).__name__}: {e}')
        results.append((label, synth, None, 0, False))
        continue
    dt = time.time() - t0
    results.append((label, synth, dt, n, True))
    print(f'{i:>3} {label:<34} {"合成" if synth else "真实":<6} {dt:>9.2f} {n:>5}')


# ---------------------------------------------------------------
# 5. 分组统计
# ---------------------------------------------------------------
def summarize(rows, title):
    times = sorted(r[2] for r in rows if r[4] and r[2] is not None)
    n = len(times)
    print(f'\n--- {title} ---')
    if n == 0:
        print('  无成功样本')
        return None
    total = sum(times)

    def pct(p):
        k = max(0, min(n - 1, int(round((p / 100) * (n - 1)))))
        return times[k]

    print(f'  成功 {n} / {len(rows)}')
    print(f'  总耗时      : {total:.1f} s')
    print(f'  平均        : {statistics.mean(times):.2f} s/页')
    print(f'  中位数(P50) : {pct(50):.2f} s/页')
    print(f'  P95         : {pct(95):.2f} s/页')
    print(f'  最快 / 最慢 : {times[0]:.2f} / {times[-1]:.2f} s')
    print(f'  吞吐        : {3600 / statistics.mean(times):.1f} 页/小时（不含加载）')
    return {
        'n': n,
        'mean': statistics.mean(times),
        'p50': pct(50),
        'p95': pct(95),
        'total': total,
    }


real_rows = [r for r in results if not r[1]]
synth_rows = [r for r in results if r[1]]

print('\n' + '=' * 64)
print('性能统计')
print('=' * 64)
real_stat = summarize(real_rows, '真实报告单')
synth_stat = summarize(synth_rows, '合成测试图')

try:
    peak = paddle.device.max_memory_allocated(DEVICE) / 1024 ** 3
    print(f'\n显存峰值: {peak:.2f} GB')
except Exception as e:
    peak = None
    print(f'\n显存查询不可用: {e}')

print(f'模型加载: {load_s:.1f} s（一次性，批量时摊销）')


# ---------------------------------------------------------------
# 6. 论文用表格
# ---------------------------------------------------------------
if real_stat:
    print('\n' + '=' * 64)
    print('可直接用于论文的表格（真实报告单）')
    print('=' * 64)
    print('| 项目 | 数值 |')
    print('| --- | --- |')
    print('| 平台 | AI Studio / Iluvatar BI-V150S 32GB |')
    print(f'| Python / paddle | 3.10.10 / {paddle.__version__} |')
    print(f'| 设备 | {DEVICE} |')
    print(f'| 样本数 | {real_stat["n"]} 份真实报告单 |')
    print(f'| 模型加载 | {load_s:.1f} s（一次性） |')
    print(f'| 平均单页推理 | {real_stat["mean"]:.2f} s |')
    print(f'| P50 / P95 | {real_stat["p50"]:.2f} s / {real_stat["p95"]:.2f} s |')
    print(f'| 吞吐 | {3600 / real_stat["mean"]:.0f} 页/小时 |')
    if peak:
        print(f'| 显存峰值 | {peak:.2f} GB |')
    print()
    print(f'对比课程版 RTX 4060 8G 的 3–5 分钟/页：')
    ratio = (180 / real_stat["mean"])
    print(f'  提速约 {ratio:.0f}~{300 / real_stat["mean"]:.0f} 倍')
else:
    print('\n⚠️ 没有测到真实报告单。合成图结果不能用于论文。')

if tmpdir:
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f'\n已清理临时目录: {tmpdir}')
