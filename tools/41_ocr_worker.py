# -*- coding: utf-8 -*-
r"""41 本地：OCR 单进程 worker（由 40_bench_ocr_parallel.py 调度，一般不单独用）

为什么单独做成一个脚本
----------------------
`multiprocessing.Pool` 在 Jupyter / AI Studio notebook 里用 spawn 模式经常挂起：
notebook 没有真正的 `__main__` 模块文件，子进程重新导入时拿不到入口，表现就是
"长时间无反应"。而且 `pool.map` 是阻塞的，中途没有任何输出，无法判断是慢还是死。

用法（一般不直接调）：
    python tools\41_ocr_worker.py --images a.png,b.png --out w0.jsonl"""

import os
import sys
import json
import time
import argparse

sys.stdout.reconfigure(encoding='utf-8')

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--images', required=True, help='逗号分隔的图片路径')
    ap.add_argument('--out', required=True, help='输出 jsonl')
    ap.add_argument('--tag', default='w', help='日志前缀')
    return ap.parse_args()


def main():
    args = parse_args()
    imgs = [x for x in args.images.split(',') if x.strip()]
    if not imgs:
        print('没有图片')
        return 1

    print('[%s] %d 张图，加载模型 (device=%s)...'
          % (args.tag, len(imgs), DEVICE), flush=True)
    t0 = time.time()
    from paddleocr import PaddleOCRVL
    pipe = PaddleOCRVL(device=DEVICE)
    print('[%s] 加载完成 %.1fs' % (args.tag, time.time() - t0), flush=True)

    records = []
    for i, img in enumerate(imgs, 1):
        t1 = time.time()
        try:
            out = list(pipe.predict(img))
            dt = time.time() - t1
            rec = {'image': os.path.basename(img), 'ok': True,
                   'dt': round(dt, 2), 'n': len(out)}
            print('[%s] %d/%d %-44s %.1fs OK'
                  % (args.tag, i, len(imgs), os.path.basename(img)[:44], dt),
                  flush=True)
        except Exception as e:
            dt = time.time() - t1
            rec = {'image': os.path.basename(img), 'ok': False,
                   'dt': round(dt, 2),
                   'err': '%s: %s' % (type(e).__name__, str(e)[:200])}
            print('[%s] %d/%d %-44s %.1fs %s'
                  % (args.tag, i, len(imgs), os.path.basename(img)[:44], dt,
                     rec['err'][:60]), flush=True)
        records.append(rec)
        # 每张都落盘，主进程可以随时读
        with open(args.out, 'w', encoding='utf-8') as fp:
            for r in records:
                fp.write(json.dumps(r, ensure_ascii=False) + '\n')

    print('[%s] 完成 %d 张，总耗时 %.1fs'
          % (args.tag, len(records), time.time() - t0), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
