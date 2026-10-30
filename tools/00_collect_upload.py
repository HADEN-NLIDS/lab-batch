# -*- coding: utf-8 -*-
"""00 本地：收集上传包（只挑要评测的图片，避免传 1.91GB 全量数据）

产出 upload_pack/ 目录，直接压缩上传即可：
    upload_pack/
    ├── README.md
    ├── requirements.txt
    ├── tools/            所有脚本
    ├── gold/             抽样后的 gold（每份一个json）
    ├── images/           抽样对应的图片
    └── upload_manifest.json

用法：
    python 00_collect_upload.py --src "<数据目录>/medrepbench" \
        --gold "<数据目录>/eval_gold_medrep" --limit 20
"""

import os
import sys
import json
import random
import shutil
import argparse

sys.stdout.reconfigure(encoding='utf-8')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--src', required=True, help='medrepbench 根目录（含 censored/）')
    ap.add_argument('--gold', required=True, help='eval_gold_medrep 目录')
    ap.add_argument('--out', default='upload_pack')
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--seed', type=int, default=20261027)
    ap.add_argument('--tools', default='tools', help='tools 目录路径')
    return ap.parse_args()


def stratified_pick(samples, limit, seed):
    """与 03_export_ocr.py 保持完全一致的分层抽样。"""
    buckets = {'小(3-5)': [], '中(6-10)': [], '大(11+)': []}
    for s in samples:
        n = s['num_items']
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
    if len(picked) < limit:
        ids = {s['report_id'] for s in picked}
        rest = [s for s in samples if s['report_id'] not in ids]
        rng.shuffle(rest)
        picked.extend(rest[:limit - len(picked)])
    return picked[:limit]


def main():
    args = parse_args()
    man = json.load(open(os.path.join(args.gold, 'manifest.json'), encoding='utf-8'))
    samples = man['samples']
    print(f'gold 共 {len(samples)} 份')

    if len(samples) <= args.limit:
        picked = samples
        print(f'样本数({len(samples)}) <= limit({args.limit})，全部选取')
    else:
        print(f'分层抽样 {args.limit} 份：')
        picked = stratified_pick(samples, args.limit, args.seed)

    os.makedirs(os.path.join(args.out, 'gold'), exist_ok=True)
    os.makedirs(os.path.join(args.out, 'images'), exist_ok=True)

    n_img = 0
    total_mb = 0.0
    for s in picked:
        rid = s['report_id']
        src_json = os.path.join(args.gold, rid + '.json')
        if os.path.isfile(src_json):
            shutil.copy2(src_json, os.path.join(args.out, 'gold', rid + '.json'))
        img_src = os.path.join(args.src, 'censored', s['image'])
        if os.path.isfile(img_src):
            shutil.copy2(img_src, os.path.join(args.out, 'images', s['image']))
            n_img += 1
            total_mb += os.path.getsize(img_src) / 1024 / 1024

    # 代码
    dst_tools = os.path.join(args.out, 'tools')
    if os.path.isdir(args.tools):
        if os.path.isdir(dst_tools):
            shutil.rmtree(dst_tools)
        shutil.copytree(args.tools, dst_tools,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    # 清单：03 脚本据此只跑这批
    json.dump({'count': len(picked), 'samples': picked},
              open(os.path.join(args.out, 'upload_manifest.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)

    # 依赖与说明
    with open(os.path.join(args.out, 'requirements.txt'), 'w', encoding='utf-8') as f:
        f.write('paddleocr[doc-parser]\npillow\n')

    readme_src = os.path.join(os.path.dirname(os.path.abspath(args.tools)), 'README.md')
    if os.path.isfile(readme_src):
        shutil.copy2(readme_src, os.path.join(args.out, 'README.md'))
    else:
        print('提示：未找到 README.md，上传包将缺少说明文件')

    print()
    print('=' * 56)
    print('上传包已生成')
    print('=' * 56)
    print(f'目录    : {os.path.abspath(args.out)}')
    print(f'样本    : {len(picked)} 份')
    print(f'图片    : {n_img} 张，共 {total_mb:.1f} MB')
    print()
    print('下一步：')
    print(f'  Compress-Archive -Path "{args.out}\\*" -DestinationPath "{args.out}.zip" -Force')
    print('  上传 zip 到 AI Studio，解压后见 README.md')


if __name__ == '__main__':
    main()
