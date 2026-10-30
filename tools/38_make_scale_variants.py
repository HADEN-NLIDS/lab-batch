# -*- coding: utf-8 -*-
r"""38 本地：生成多档像素缩放变体，用于测试"识别效果与输入分辨率的关系"

要回答的问题
------------
`144502` 的「单位」列（`mmol/L` 之类）4 次 OCR 都读不出来，但人眼可见。
是分辨率不够，还是模型能力上限？

用法
----
    # 只测两张关键图（推荐先这样）
    python tools\38_make_scale_variants.py ^
        --images "<图片目录>" ^
        --only 144502 094238 ^
        --out-dir "<数据目录>\scale_test" """

import os
import sys
import json
import glob
import argparse

sys.stdout.reconfigure(encoding='utf-8')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--images', required=True, help='原图目录')
    ap.add_argument('--out-dir', default='scale_test')
    ap.add_argument('--scales', default='0.75,1,1.5,2,3',
                    help='缩放档位，逗号分隔（1 表示原图，会重存一次保证编码一致）')
    ap.add_argument('--only', nargs='*', default=[],
                    help='只处理文件名含这些关键词的图')
    ap.add_argument('--ext', default='png', choices=['png', 'jpg'],
                    help='输出格式。jpg 体积小但会引入压缩损失，测像素关系建议用 png')
    return ap.parse_args()


def main():
    args = parse_args()
    try:
        from PIL import Image
    except ImportError:
        print('需要 Pillow')
        return 1

    scales = []
    for s in args.scales.split(','):
        s = s.strip()
        if not s:
            continue
        try:
            scales.append(float(s))
        except ValueError:
            print('跳过无效档位:', s)
    if not scales:
        print('没有有效档位')
        return 1

    imgs = sorted([f for f in glob.glob(os.path.join(args.images, '*'))
                   if os.path.isfile(f)
                   and f.lower().endswith(('.png', '.jpg', '.jpeg'))])
    if args.only:
        imgs = [f for f in imgs
                if any(k in os.path.basename(f) for k in args.only)]
    if not imgs:
        print('没有匹配的图片')
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    records = []
    print('原图 %d 张，档位 %s\n' % (len(imgs), scales))
    print('%-40s %-8s %-14s %s' % ('输出文件', '档位', '输出尺寸', '文件大小'))
    print('-' * 78)

    for f in imgs:
        stem = os.path.splitext(os.path.basename(f))[0]
        im = Image.open(f).convert('RGB')
        W, H = im.size
        for sc in scales:
            nw, nh = int(round(W * sc)), int(round(H * sc))
            out = im.resize((nw, nh), Image.LANCZOS) if sc != 1.0 else im
            tag = ('%g' % sc).replace('.', '_')
            name = '%s__x%s.%s' % (stem, tag, args.ext)
            path = os.path.join(args.out_dir, name)
            if args.ext == 'jpg':
                out.save(path, quality=95)
            else:
                out.save(path)
            size = os.path.getsize(path)
            records.append({
                'file': name,
                'source': os.path.basename(f),
                'stem': stem,
                'scale': sc,
                'src_size': [W, H],
                'out_size': [nw, nh],
                'bytes': size,
            })
            print('%-40s %-8s %-14s %.0f KB'
                  % (name, '%gx' % sc, '%dx%d' % (nw, nh), size / 1024))

    man = {
        'note': ('同一批图的不同像素档位，用于测试识别效果与输入分辨率的关系。'
                 '文件名格式 <原图名>__x<档位>.<ext>，39_compare_scales.py 靠它分组。'),
        'scales': scales,
        'count': len(records),
        'files': records,
    }
    with open(os.path.join(args.out_dir, 'manifest.json'), 'w',
              encoding='utf-8') as fp:
        json.dump(man, fp, ensure_ascii=False, indent=1)

    print()
    print('生成 %d 个文件 → %s' % (len(records), args.out_dir))
    print()
    print('下一步：')
    print('  1. 把 %s 整个目录传到 AI Studio' % args.out_dir)
    print('  2. python tools/03_export_ocr.py --gold-dir gold --src %s '
          '--out-dir artifacts_scale' % os.path.basename(args.out_dir))
    print('  3. 下载 artifacts_scale 回来')
    print('  4. python tools\\39_compare_scales.py --artifacts artifacts_scale '
          '--gold "<数据目录>\\自采数据集\\gold"')
    return 0


if __name__ == '__main__':
    sys.exit(main())
