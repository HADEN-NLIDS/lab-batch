# -*- coding: utf-8 -*-
"""40 本地：OCR 并发/批处理能力测试（在 AI Studio 上跑）

要回答三个问题
--------------
1. **瓶颈在哪**：单图 30–60s，GPU 到底忙不忙？忙 → 加并发没用；闲着 → 加并发有用
2. **PaddleOCR-VL 支持批处理吗**：`predict(img)` 能不能吃 list？这决定用哪种方案
3. **多进程能开几个**：每进程约 5.8G，32G 卡的实际上限是多少

用法
----
    # 在 AI Studio 上，把 scale_test 或原图目录传上去
    python tools/40_bench_ocr_parallel.py --images ./scale_test --probe
    python tools/40_bench_ocr_parallel.py --images ./scale_test --workers 1 2 3"""

import os
import sys
import time
import glob
import json
import subprocess
import argparse

sys.stdout.reconfigure(encoding='utf-8')

# 必须和 03_export_ocr.py 用同一套设备初始化。
# 漏掉这三行的后果：PADDLE_DEVICE 未设置 → device 传 None → PaddleOCR
# 回退到 CPU 推理，一张图要跑好几分钟，看起来像"卡死"。
# 漏掉 MODEL_SOURCE 还会去 HuggingFace 拉模型，在国内网络下直接超时。
os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--images', required=True, help='图片目录')
    ap.add_argument('--images-limit', type=int, default=0, help='最多用几张图')
    ap.add_argument('--probe', action='store_true',
                    help='探测 predict 是否支持 list 输入（批处理可行性）')
    ap.add_argument('--util', action='store_true',
                    help='处理单张图并采样 GPU 利用率，判断瓶颈')
    ap.add_argument('--workers', nargs='*', type=int, default=[],
                    help='要测的多进程并发度，如 1 2 3')
    ap.add_argument('--out', default='ocr_parallel.json')
    return ap.parse_args()


def list_images(d, limit=0):
    imgs = sorted(f for f in glob.glob(os.path.join(d, '*'))
                  if f.lower().endswith(('.png', '.jpg', '.jpeg')))
    return imgs[:limit] if limit else imgs


def gpu_snapshot():
    """尽力拿 GPU 利用率/显存。

    顺序：各家 SMI 命令行 -> paddle 自己的显存统计。
    Iluvatar 卡上不一定有 ixsmi，所以不能只依赖它；拿不到也不算失败，
    并发测试的提速比本身就能反推瓶颈在哪。
    """
    for tool in ('ixsmi', 'nvidia-smi', 'ix-smi', 'ixsmi-smi', 'dsmi'):
        try:
            r = subprocess.run(
                [tool, '--query-gpu=utilization.gpu,memory.used,memory.total',
                 '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=6)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().split(chr(10))[0]
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue

    # 退一步：paddle 自己的统计（自定义设备上不一定实现，失败就算了）
    try:
        import paddle
        alloc = paddle.device.cuda.memory_allocated()
        peak = paddle.device.cuda.max_memory_allocated()
        return 'paddle_mem alloc=%.0fMiB peak=%.0fMiB' % (alloc / 2 ** 20,
                                                          peak / 2 ** 20)
    except Exception:
        pass
    return ''


def probe_batch():
    """探测 pipeline 是否接受 list 输入。"""
    print('=' * 70)
    print('探测批处理支持')
    print('=' * 70)
    from paddleocr import PaddleOCRVL
    print('  加载模型 (device=%s) ...' % DEVICE)
    pipe = PaddleOCRVL(device=DEVICE)

    # 列出可能的批处理入口
    cands = []
    for name in dir(pipe):
        low = name.lower()
        if 'predict' in low or 'batch' in low or 'ocr' in low:
            if not name.startswith('_'):
                cands.append(name)
    print('  pipeline 上相关方法/属性:')
    for c in sorted(cands):
        print('    .%s' % c)

    print()
    print('  predict 签名:')
    try:
        import inspect
        for c in ('predict', 'predict_batch'):
            fn = getattr(pipe, c, None)
            if fn:
                print('    %s%s' % (c, inspect.signature(fn)))
    except Exception as e:
        print('    读签名失败:', e)

    # 实测：传 list 会不会被接受
    imgs = list_images(args.images, 2)
    if len(imgs) >= 2:
        print()
        print('  实测：predict([img1, img2]) ...')
        try:
            out = list(pipe.predict(imgs))
            print('    接受 list，返回 %d 个结果 → 原生支持批处理' % len(out))
        except Exception as e:
            print('    不接受 list：%s: %s' % (type(e).__name__, str(e)[:120]))
            print('    → 无法原地批处理，只能走多进程')


def run_util(img):
    """单张图，处理期间采样 GPU 利用率。"""
    print('=' * 70)
    print('单图瓶颈探测')
    print('=' * 70)
    from paddleocr import PaddleOCRVL
    print('  加载模型 (device=%s) ...' % DEVICE)
    t_load = time.time()
    pipe = PaddleOCRVL(device=DEVICE)
    print('  加载完成 %.1fs' % (time.time() - t_load))
    print()
    print('  处理 %s' % os.path.basename(img))
    print('    %-8s %s' % ('t(s)', '状态'))
    t0 = time.time()
    samples = []
    import threading
    got_gpu = [False]

    def sampler():
        warned = False
        last_tick = -99
        while not stop[0]:
            t = time.time() - t0
            snap = gpu_snapshot()
            if snap:
                got_gpu[0] = True
                samples.append((t, snap))
                print('    %-8.1f util/mem = %s' % (t, snap))
            else:
                # 拿不到 GPU 数据时也要有心跳，否则看起来像卡死
                if not warned:
                    print('    %-8.1f 拿不到 GPU 状态（ixsmi / nvidia-smi 都不可用），'
                          '只报进度' % t)
                    warned = True
                elif t - last_tick >= 10:
                    print('    %-8.1f 仍在处理…' % t)
                    last_tick = t
            time.sleep(2)

    stop = [False]
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    try:
        list(pipe.predict(img))
    finally:
        stop[0] = True
    dt = time.time() - t0
    th.join(timeout=3)

    print()
    print('  单图耗时 %.1fs' % dt)
    utils = []
    for _, s in samples:
        parts = [p.strip() for p in s.split(',')]
        if parts and parts[0].isdigit():
            utils.append(int(parts[0]))
    if utils:
        avg = sum(utils) / len(utils)
        print('  GPU 利用率：平均 %.0f%%，最高 %d%%' % (avg, max(utils)))
        print()
        if avg < 50:
            print('  → 利用率偏低：瓶颈不在 GPU 计算，批处理/并发**值得做**')
        elif avg < 80:
            print('  → 利用率中等：批处理可能有中等收益')
        else:
            print('  → 利用率已高：加并发收益有限，优先考虑别的优化（如降低输出长度）')
    else:
        print('  没拿到 GPU 数据。两种办法：')
        print('    1. 另一个终端手动观察：`ixsmi` 或 `nvidia-smi -l 1`')
        print('    2. 跳过利用率，直接测并发（结论更直接）：')
        print('       python tools/40_bench_ocr_parallel.py --images %s '
              '--workers 1 2' % args.images)


def run_workers(imgs, worker_counts, out_dir='_ocr_workers'):
    """用独立子进程测并发。

    为什么不用 multiprocessing.Pool：spawn 模式在 Jupyter / AI Studio notebook
    里会挂起（notebook 没有真实的 __main__ 模块文件），而且 pool.map 阻塞期间
    没有任何输出，无法判断是慢还是死。实测就是你遇到的"长时间无反应"。"""
    import subprocess
    import shutil

    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    worker_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '41_ocr_worker.py')

    print('=' * 70)
    print('多进程并发测试（subprocess 方式）')
    print('=' * 70)
    print('  图片 %d 张，并发度 %s' % (len(imgs), worker_counts))
    print('  模型加载约 5s/进程（已缓存时），单图约 36s，请注意预期时间')
    print()

    results = []
    for w in worker_counts:
        print('-' * 70)
        print('  并发度 %d：把 %d 张图分成 %d 份' % (w, len(imgs), w))
        print('-' * 70)
        # 轮转分配，避免某一份全是复杂图
        chunks = [[] for _ in range(w)]
        for i, img in enumerate(imgs):
            chunks[i % w].append(img)
        chunks = [c for c in chunks if c]

        procs = []
        t0 = time.time()
        for k, chunk in enumerate(chunks):
            out_jsonl = os.path.join(out_dir, 'w%d_%d.jsonl' % (w, k))
            log_path = os.path.join(out_dir, 'w%d_%d.log' % (w, k))
            logf = open(log_path, 'w', encoding='utf-8')
            cmd = [sys.executable, worker_py,
                   '--images', ','.join(chunk),
                   '--out', out_jsonl,
                   '--tag', 'w%d' % k]
            procs.append({
                'k': k, 'chunk': chunk, 'out': out_jsonl, 'log': log_path,
                'proc': subprocess.Popen(cmd, stdout=logf, stderr=logf),
                'logf': logf,
            })
            print('    启动 worker %d：%d 张图' % (k, len(chunk)), flush=True)

        # 轮询，每 10 秒报一次进度
        last_report = 0
        while True:
            alive = [p for p in procs if p['proc'].poll() is None]
            if not alive:
                break
            el = time.time() - t0
            if el - last_report >= 10:
                done = 0
                for p in procs:
                    if os.path.isfile(p['out']):
                        with open(p['out'], encoding='utf-8') as fp:
                            done += sum(1 for _ in fp)
                print('    %5.1fs 已跑完 %d/%d 张，%d 个 worker 仍在运行'
                      % (el, done, len(imgs), len(alive)), flush=True)
                last_report = el
            time.sleep(2)

        dt = time.time() - t0
        for p in procs:
            p['logf'].close()

        # 汇总
        ok = fail = 0
        item_sum = 0.0
        for p in procs:
            if not os.path.isfile(p['out']):
                fail += len(p['chunk'])
                continue
            with open(p['out'], encoding='utf-8') as fp:
                for line in fp:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if r.get('ok'):
                        ok += 1
                        item_sum += r.get('dt') or 0
                    else:
                        fail += 1
        avg = item_sum / ok if ok else 0
        thr = ok / dt * 60 if dt else 0
        print()
        print('  → 并发 %d：总耗时 %.1fs，成功 %d 失败 %d，'
              '单图平均 %.1fs，吞吐 %.1f 份/分钟'
              % (w, dt, ok, fail, avg, thr))
        # 任一 worker 没产出就别急着下结论
        no_out = [p for p in procs if not os.path.isfile(p['out'])]
        if no_out:
            print('    !! %d 个 worker 没有任何输出，可能是子进程启动失败'
                  % len(no_out))
            for p in no_out[:2]:
                print('      日志 %s 末尾：' % p['log'])
                if os.path.isfile(p['log']):
                    with open(p['log'], encoding='utf-8') as fp:
                        tail = fp.read().strip().split('\n')[-4:]
                    for line in tail:
                        print('        ' + line[:100])
        print()

        results.append({
            'workers': w, 'wall_s': round(dt, 1), 'ok': ok, 'fail': fail,
            'item_avg_s': round(avg, 1), 'throughput_per_min': round(thr, 2),
            'no_output_workers': len(no_out),
        })

    scored = [r for r in results if r['ok'] and not r['no_output_workers']]
    if len(scored) >= 2:
        base = next((r for r in scored if r['workers'] == 1), None)
        if base:
            print('=' * 70)
            print('提速比（以 1 并发为基准）')
            print('=' * 70)
            for r in scored:
                print('  %d 并发：%.2fx   吞吐 %.1f 份/分钟'
                      % (r['workers'], base['wall_s'] / r['wall_s'],
                         r['throughput_per_min']))
            best = max(scored, key=lambda x: x['throughput_per_min'])
            print()
            print('  最优并发度：%d（%.1f 份/分钟）'
                  % (best['workers'], best['throughput_per_min']))
            if best['workers'] == 1 or best['wall_s'] >= base['wall_s'] * 0.9:
                print('  提速不明显 → GPU 可能已接近饱和，加并发收益有限')
            else:
                print('  提速明显 → GPU 有余量，并发值得做')
    return results


def main():
    global args
    args = parse_args()
    imgs = list_images(args.images, args.images_limit)
    if not imgs:
        print('没有图片:', args.images)
        return 1
    print('图片 %d 张：%s' % (len(imgs), [os.path.basename(x) for x in imgs[:5]]))
    print()

    if args.probe:
        probe_batch()
        return 0
    if args.util:
        run_util(imgs[0])
        return 0
    if args.workers:
        rs = run_workers(imgs, args.workers)
        with open(args.out, 'w', encoding='utf-8') as fp:
            json.dump({'images': len(imgs), 'results': rs}, fp,
                      ensure_ascii=False, indent=1)
        print()
        print('结果写入', args.out)
        return 0

    print('需要 --probe / --util / --workers 之一')
    return 1


if __name__ == '__main__':
    sys.exit(main())
