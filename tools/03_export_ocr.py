# -*- coding: utf-8 -*-
"""阶段1：对 MedRepBench 样本跑 OCR，导出原始输出到 artifacts

为什么不直接输出 predictions：
    PaddleOCR-VL 单张约 50 秒，抽取逻辑一旦写错，重跑成本极高。
    先导出完整原始输出，后续解析可在本地反复迭代，不占 GPU。

用法（AI Studio Notebook 粘贴或 !python）：
    python run_ocr_export.py --gold-dir ./eval_gold_medrep --src ./medrepbench --limit 20"""

import os
import sys
import json
import time
import glob
import random
import argparse

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--gold-dir', default='gold',
                    help='gold 目录（上传包内为 ./gold，本地为 eval_gold_medrep）')
    ap.add_argument('--src', default='.',
                    help='根目录。上传包内为 .（含 images/），本地为 medrepbench（含 censored/）')
    ap.add_argument('--scan-src', action='store_true',
                    help='不读 manifest，直接扫描 --src 目录里的所有图片作为待处理样本。'
                         '适合跑变体图、单批实验等没有 gold 清单的场景。')
    ap.add_argument('--out-dir', default='artifacts_export')
    ap.add_argument('--limit', type=int, default=20, help='本次最多跑几份')
    ap.add_argument('--seed', type=int, default=20261027, help='采样随机种子，保证可复现')
    ap.add_argument('--stratify', action='store_true', default=True,
                    help='按项目数分层抽样（默认开启）')
    ap.add_argument('--report-ids', default='',
                    help='指定report_id，逗号分隔，优先于抽样')
    return ap.parse_args()


def resolve_dirs(src):
    """自动识别图片目录：上传包用 images/，本地全量用 censored/。"""
    for cand in ('images', 'censored'):
        d = os.path.join(src, cand)
        if os.path.isdir(d):
            return d, cand
    return os.path.join(src, 'censored'), 'censored(不存在)'


def scan_src_dir(src):
    """把 src 目录里的所有图片当作待处理样本。

    为什么需要：原来的流程是 manifest 驱动——样本列表来自 gold 的 manifest，
    --src 只负责在 images/ 或 censored/ 子目录里找同名图。一旦要跑变体图
    （如 scale_test 里的 xxx__x2.png）或任何没有 gold 清单的批次，
    manifest 和图片目录对不上，就会全部在 0.0s 报 RuntimeError。"""
    d = src
    imgs_in = [f for f in os.listdir(d)
               if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
    if not imgs_in:
        d, _ = resolve_dirs(src)
        imgs_in = [f for f in os.listdir(d)
                   if f.lower().endswith(('.png', '.jpg', '.jpeg'))] if os.path.isdir(d) else []
    if not imgs_in:
        print('目录里没有图片:', src)
        sys.exit(1)
    files = sorted(imgs_in)
    samples = [{'report_id': os.path.splitext(f)[0], 'image': f, 'num_items': None}
               for f in files]
    print(f'扫描目录: {d}（{len(files)} 张图）')
    return samples, d


def load_manifest(gold_dir):
    """优先读 upload_manifest.json（上传包），否则读 manifest.json（本地全量）。"""
    for name in ('upload_manifest.json', 'manifest.json'):
        path = os.path.join(gold_dir, name)
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            print(f'清单: {name}（{len(data["samples"])} 份）')
            return data['samples']
    # 上传包结构：upload_manifest.json 在根目录，gold/ 是子目录
    path = 'upload_manifest.json'
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        print(f'清单: {path}（{len(data["samples"])} 份）')
        return data['samples']
    print('找不到 manifest.json / upload_manifest.json，已搜索:', gold_dir)
    sys.exit(1)


def stratified_pick(samples, limit, seed):
    """按项目数分三档抽样：3-5 / 6-10 / 11+，避免只抽到简单或复杂报告。"""
    buckets = {'小(3-5)': [], '中(6-10)': [], '大(11+)': []}
    for s in samples:
        n = s.get('num_items') or 0
        if n <= 5:
            buckets['小(3-5)'].append(s)
        elif n <= 10:
            buckets['中(6-10)'].append(s)
        else:
            buckets['大(11+)'].append(s)

    rng = random.Random(seed)
    picked = []
    per = max(1, limit // 3)
    for name, arr in buckets.items():
        rng.shuffle(arr)
        take = arr[:per]
        picked.extend(take)
        print(f'  分层 {name}: 共{len(arr)}份，抽{len(take)}份')
    # 不足时从剩余里补
    if len(picked) < limit:
        rest = [s for s in samples if s not in picked]
        rng.shuffle(rest)
        picked.extend(rest[:limit - len(picked)])
    return picked[:limit]


def to_jsonable(obj, depth=0):
    """把产线输出对象转成可序列化的结构，保留关键字段。"""
    if depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x, depth + 1) for x in obj]
    # 官方 result 对象：优先取已知属性
    out = {}
    for attr in ('json', 'markdown'):
        try:
            if hasattr(obj, attr):
                out[attr] = to_jsonable(getattr(obj, attr), depth + 1)
        except Exception as e:
            out[attr] = f'<读取失败: {e}>'
    if out:
        return out
    return str(obj)


def main():
    args = parse_args()
    if args.scan_src:
        samples, scan_dir = scan_src_dir(args.src)
    else:
        samples = load_manifest(args.gold_dir)

    if args.report_ids:
        wanted = {x.strip() for x in args.report_ids.split(',') if x.strip()}
        picked = [s for s in samples if s['report_id'] in wanted]
        print(f'按指定 report_id 选取: {len(picked)} 份')
    elif args.scan_src:
        # scan 模式没有 num_items 信息，分层抽样没有意义——按顺序取前 limit 个
        picked = samples[:args.limit] if args.limit else samples
        print(f'scan 模式：处理 {len(picked)}/{len(samples)} 份（按名称顺序）')
    elif len(samples) <= args.limit:
        print(f'清单共 {len(samples)} 份 <= limit({args.limit})，全部处理')
        picked = samples
    else:
        print(f'分层抽样，目标 {args.limit} 份:')
        picked = stratified_pick(samples, args.limit, args.seed)

    os.makedirs(args.out_dir, exist_ok=True)
    if args.scan_src:
        img_dir = scan_dir
    else:
        img_dir, img_sub = resolve_dirs(args.src)
    print(f'图片目录: {img_dir}')

    # 断点续跑：跳过已导出的
    todo = [s for s in picked
            if not os.path.isfile(os.path.join(args.out_dir, s['report_id'] + '.json'))]
    skipped = len(picked) - len(todo)
    if skipped:
        print(f'已有 {skipped} 份导出，跳过')
    if not todo:
        print('本次无需处理，全部已存在')
        return

    from paddleocr import PaddleOCRVL
    print(f'\n加载 PaddleOCR-VL (device={DEVICE}) ...')
    t0 = time.time()
    pipeline = PaddleOCRVL(device=DEVICE)
    print(f'加载完成 {time.time() - t0:.1f}s\n')

    index_path = os.path.join(args.out_dir, '_index.json')
    index = {}
    if os.path.isfile(index_path):
        with open(index_path, encoding='utf-8') as f:
            index = json.load(f)

    ok = fail = 0
    print(f'{"#":>3} {"report_id":<34} {"项数":>5} {"耗时":>8} {"状态":>6}')
    print('-' * 62)

    for i, s in enumerate(todo, 1):
        rid = s['report_id']
        img = os.path.join(img_dir, s['image'])
        # scan 模式下没有 gold 项数，num_items 是 None。
        # 直接用 {None:>5} 格式化会抛 TypeError，把成功的 OCR 也带进 except 分支。
        n_gt = s.get('num_items')
        n_disp = str(n_gt) if n_gt is not None else '-'
        t1 = time.time()
        try:
            out = list(pipeline.predict(img))
            dt = time.time() - t1
            doc = {
                'report_id': rid,
                'image': s['image'],
                'num_items_gt': s.get('num_items'),
                'device': DEVICE,
                'elapsed_s': round(dt, 2),
                'results': to_jsonable(out),
            }
            with open(os.path.join(args.out_dir, rid + '.json'), 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False)
            index[rid] = {'elapsed_s': round(dt, 2), 'num_items_gt': n_gt,
                          'image': s['image'], 'ok': True}
            ok += 1
            print(f'{i:>3} {rid:<34} {n_disp:>5} {dt:>7.1f}s {"OK":>6}')
        except Exception as e:
            dt = time.time() - t1
            index[rid] = {'elapsed_s': round(dt, 2), 'num_items_gt': n_gt,
                          'image': s['image'], 'ok': False, 'error': f'{type(e).__name__}: {e}'}
            fail += 1
            print(f'{i:>3} {rid:<34} {n_disp:>5} {dt:>7.1f}s {type(e).__name__:>6}')
            continue

        # 每份都落盘索引，防止中途断开丢失进度
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=1)

    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=1)

    times = [v['elapsed_s'] for v in index.values() if v.get('ok')]
    print('\n' + '=' * 58)
    print(f'完成：成功 {ok}，失败 {fail}，本次共导出 {len(glob.glob(os.path.join(args.out_dir, "*.json")))-1} 份')
    if times:
        print(f'耗时统计：平均 {sum(times)/len(times):.1f}s  最快 {min(times):.1f}s  最慢 {max(times):.1f}s')
    print(f'输出目录：{os.path.abspath(args.out_dir)}')
    print()
    print('下一步：把 artifacts_export/ 下载到本地，运行')
    print('    python parse_predictions.py --artifacts ./artifacts_export --gold-dir ./eval_gold_medrep')


if __name__ == '__main__':
    main()
