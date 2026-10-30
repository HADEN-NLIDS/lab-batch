# -*- coding: utf-8 -*-
"""26 云端：对任意图片目录跑 OCR，导出 artifacts（不需要 gold）

为什么单独写：
    03_export_ocr.py 依赖 gold/manifest.json 决定处理哪些图。
    自采图片没有 gold，需要这个"只给图片目录"的版本。

用法（AI Studio）：
    python tools/26_ocr_images_only.py --images ./myimages --out ./artifacts_self"""

import os
import sys
import json
import time
import glob
import argparse

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--images', default='images')
    ap.add_argument('--out', default='artifacts_self')
    ap.add_argument('--max-tables', type=int, default=8)
    return ap.parse_args()


def to_jsonable(obj, depth=0):
    """把产线输出转成可序列化结构（与 03 一致）。"""
    if depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x, depth + 1) for x in obj]
    out = {}
    for attr in ('json', 'markdown'):
        try:
            if hasattr(obj, attr):
                out[attr] = to_jsonable(getattr(obj, attr), depth + 1)
        except Exception as e:
            out[attr] = f'<读取失败: {e}>'
    return out if out else str(obj)


def main():
    args = parse_args()
    imgs = []
    for ext in ('*.jpg', '*.jpeg', '*.png', '*.bmp', '*.tif', '*.tiff'):
        imgs.extend(glob.glob(os.path.join(args.images, ext)))
        imgs.extend(glob.glob(os.path.join(args.images, ext.upper())))
    imgs = sorted(set(imgs))
    if not imgs:
        print(f'{args.images} 下没有图片')
        sys.exit(1)
    print(f'待处理 {len(imgs)} 张')

    os.makedirs(args.out, exist_ok=True)

    # 断点续跑
    todo = [p for p in imgs
            if not os.path.isfile(os.path.join(
                args.out, os.path.splitext(os.path.basename(p))[0] + '.json'))]
    if len(todo) < len(imgs):
        print(f'已存在 {len(imgs)-len(todo)} 份，跳过')
    if not todo:
        print('全部已处理完')
        return

    from paddleocr import PaddleOCRVL
    import paddle
    paddle.set_device(DEVICE)

    print(f'加载 PaddleOCR-VL (device={DEVICE}) ...')
    t0 = time.time()
    pipeline = PaddleOCRVL(device=DEVICE)
    print(f'加载完成 {time.time()-t0:.1f}s\n')

    index_path = os.path.join(args.out, '_index.json')
    index = {}
    if os.path.isfile(index_path):
        index = json.load(open(index_path, encoding='utf-8'))

    ok = fail = 0
    print(f'{"#":>3} {"文件":<40} {"耗时":>8} {"状态":>6}')
    print('-' * 62)
    for i, p in enumerate(todo, 1):
        name = os.path.basename(p)
        rid = os.path.splitext(name)[0]
        t1 = time.time()
        try:
            out = list(pipeline.predict(p))
            dt = time.time() - t1
            doc = {
                'report_id': rid,
                'image': name,
                'device': DEVICE,
                'elapsed_s': round(dt, 2),
                'results': to_jsonable(out),
            }
            with open(os.path.join(args.out, rid + '.json'), 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False)
            index[rid] = {'elapsed_s': round(dt, 2), 'image': name, 'ok': True}
            ok += 1
            print(f'{i:>3} {name[:38]:<40} {dt:>7.1f}s {"OK":>6}')
        except Exception as e:
            dt = time.time() - t1
            index[rid] = {'elapsed_s': round(dt, 2), 'image': name,
                          'ok': False, 'error': f'{type(e).__name__}: {e}'}
            fail += 1
            print(f'{i:>3} {name[:38]:<40} {dt:>7.1f}s {type(e).__name__:>6}')
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=1)

    times = [v['elapsed_s'] for v in index.values() if v.get('ok')]
    print('\n' + '=' * 58)
    print(f'完成：成功 {ok}，失败 {fail}')
    if times:
        print(f'耗时：平均 {sum(times)/len(times):.1f}s  '
              f'最快 {min(times):.1f}s  最慢 {max(times):.1f}s')
    print(f'输出：{os.path.abspath(args.out)}')
    print()
    print('下一步：把整个 artifacts 目录下载到本地，然后运行')
    print('  python tools\\25_make_preannotation.py --artifacts <下载的目录> '
          '--images <图片目录>')


if __name__ == '__main__':
    main()
