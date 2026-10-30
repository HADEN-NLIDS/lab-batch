# -*- coding: utf-8 -*-
r"""37 本地：OCR 重试——对读不出细节的图生成变体，用于定位"是图的问题还是模型的问题"

什么时候用
----------
某张图的某一列稳定读不出来（重跑几次都一样），但人眼看得清。
`144502` 就是这样：表头有「单位」，32 行数据的单位格全是空的，
重跑 OCR 也复现。这类情况通常不是解析器的问题，也不是表格结构的问题，
而是**这一列的字太小/太淡，没达到 OCR 的检测阈值**。

用法
----
    python tools\37_ocr_retry.py --image "C:\...\144502.png" --out-dir retry"""

import os
import sys
import argparse

sys.stdout.reconfigure(encoding='utf-8')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--image', required=True)
    ap.add_argument('--out-dir', default='ocr_retry')
    ap.add_argument('--scale', type=float, default=2.0)
    ap.add_argument('--overlap', type=float, default=0.12,
                    help='分块时的重叠比例，避免把一行切成两半')
    return ap.parse_args()


def main():
    args = parse_args()
    try:
        from PIL import Image, ImageEnhance, ImageOps
    except ImportError:
        print('需要 Pillow：pip install Pillow')
        return 1

    if not os.path.isfile(args.image):
        print('找不到图片:', args.image)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)

    im = Image.open(args.image).convert('RGB')
    stem = os.path.splitext(os.path.basename(args.image))[0]
    W, H = im.size
    print('原图 %dx%d  %.1f KB' % (W, H, os.path.getsize(args.image) / 1024))
    print()

    made = []

    # 1) 2 倍上采样
    big = im.resize((int(W * args.scale), int(H * args.scale)), Image.LANCZOS)
    p = os.path.join(args.out_dir, '%s_2x.png' % stem)
    big.save(p)
    made.append((p, '2 倍上采样 %dx%d' % big.size))

    # 2) 灰度 + 对比度增强（小字发灰时有用）
    g = ImageOps.grayscale(im)
    g = ImageEnhance.Contrast(g).enhance(1.8)
    g = g.convert('RGB')
    p = os.path.join(args.out_dir, '%s_gray.png' % stem)
    g.save(p)
    made.append((p, '灰度 + 对比度 1.8'))

    # 3) 上下分块（带重叠）
    ov = int(H * args.overlap)
    mid = H // 2
    for tag, box in (('top', (0, 0, W, mid + ov)),
                     ('bottom', (0, mid - ov, W, H))):
        part = im.crop(box)
        part = part.resize((int(part.width * args.scale),
                            int(part.height * args.scale)), Image.LANCZOS)
        p = os.path.join(args.out_dir, '%s_%s.png' % (stem, tag))
        part.save(p)
        made.append((p, '分块 %s（重叠 %d px）%dx%d'
                     % (tag, ov, part.width, part.height)))

    print('生成 %d 个变体：' % len(made))
    for p, desc in made:
        print('  %-46s %-28s %.1f KB'
              % (os.path.basename(p), desc, os.path.getsize(p) / 1024))

    print()
    print('下一步：把 %s 传到 AI Studio，跑' % args.out_dir)
    print('  python tools/03_export_ocr.py --gold-dir gold --src %s '
          '--out-dir artifacts_retry' % args.out_dir)
    print('然后看 artifacts_retry 里哪份的表格能读出目标列。')
    print()
    print('判断标准：')
    print('  · 某个变体读出来了 → 是检测阈值问题，对这类图固定用该变体')
    print('  · 全部读不出来     → 图像信息确实不足，接受该列缺失')
    return 0


if __name__ == '__main__':
    sys.exit(main())
