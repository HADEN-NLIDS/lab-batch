# -*- coding: utf-8 -*-
r"""42 本地：OCR 提速配置测试（在 AI Studio 上跑）

背景
----
实测结论：像素面积差 16 倍、耗时只差 15% → 瓶颈在**文本解码**，不在视觉编码。
所以这一轮只测"能减少解码量"或"能重叠计算"的配置，不再碰分辨率。

用法
----
    # 1. 先探测本机 API 支持哪些参数
    python tools\42_ocr_optimize_test.py --images ./原图目录 --mode probe"""

import os
import sys
import time
import glob
import json
import argparse
import inspect

sys.stdout.reconfigure(encoding='utf-8')

os.environ.setdefault('PADDLE_XCCL_BACKEND', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_DEVICE', 'iluvatar_gpu')
os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'aistudio')

DEVICE = os.environ['PADDLE_DEVICE']


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--images', required=True)
    ap.add_argument('--mode', default='test', choices=['probe', 'test'])
    ap.add_argument('--limit', type=int, default=4, help='每组用几张图')
    ap.add_argument('--only', nargs='*', default=[],
                    help='只跑这些配置，如 --only baseline table_only。'
                         '配置全名：baseline / table_only / queues / '
                         'maxtok_1024 / batch4')
    ap.add_argument('--out', default='ocr_optimize.json')
    return ap.parse_args()


def list_images(d, limit=0):
    imgs = sorted(f for f in glob.glob(os.path.join(d, '*'))
                  if f.lower().endswith(('.png', '.jpg', '.jpeg')))
    return imgs[:limit] if limit else imgs


def _p4():
    """惰性加载 04 的解析器，用来把 OCR 输出转成项目列表。"""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        'p04_for42', os.path.join(here, '04_parse_predictions.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def count_items(out):
    """统计 OCR 输出规模，并用 04 解析出实际项目数。

    只用"表格行数"不够——还要看解析器能不能从里面抽出项目，
    毕竟下游用的是解析结果而不是原始 HTML。
    """
    p4 = _p4()
    n_blocks = n_rows = n_items = n_unit = 0
    for res in out:
        blocks = None
        try:
            blocks = res['parsing_res_list']
        except Exception:
            try:
                blocks = res.parsing_res_list
            except Exception:
                blocks = []
        blocks = blocks or []
        n_blocks += len(blocks)
        for b in blocks:
            content = ''
            try:
                content = b['content'] or ''
            except Exception:
                content = getattr(b, 'content', '') or ''
            if '<tr' in content:
                n_rows += content.count('<tr')
        # 复用 04：把 blocks 包成它能读的 artifacts 结构
        fake = {'results': [{'parsing_res_list': [
            '#################\nlabel:\t%s\ncontent:\t%s\n#################'
            % (getattr(bl, 'label', 'text') if not isinstance(bl, dict)
               else bl.get('label', 'text'),
               content_of(bl))
            for bl in blocks]}]}
        try:
            for tb in p4.extract_tables(p4.get_html_sources(fake)):
                for row in tb:
                    for it in p4.extract_row_items(row):
                        if it.get('item_name'):
                            n_items += 1
                            if (it.get('item_unit') or '').strip():
                                n_unit += 1
        except Exception:
            pass
    return {'blocks': n_blocks, 'table_rows': n_rows,
            'items': n_items, 'units': n_unit}


def content_of(bl):
    try:
        return bl['content'] or ''
    except Exception:
        return getattr(bl, 'content', '') or ''


def probe():
    from paddleocr import PaddleOCRVL
    print('=' * 74)
    print('API 探测')
    print('=' * 74)

    print('\n[1] PaddleOCRVL.__init__ 签名：')
    try:
        print('   ', inspect.signature(PaddleOCRVL.__init__))
    except Exception as e:
        print('    读不到:', e)

    pipe = PaddleOCRVL(device=DEVICE)
    print('\n[2] predict 签名：')
    try:
        print('   ', inspect.signature(pipe.predict))
    except Exception as e:
        print('    读不到:', e)

    print('\n[3] 关键属性（看 batch_size / use_queues 的当前值）：')
    for attr in ('batch_size', 'use_queues', 'use_layout_detection',
                 'use_chart_recognition', 'use_seal_recognition',
                 'format_block_content', 'markdown_ignore_labels',
                 'use_ocr_for_image_block', 'merge_layout_blocks'):
        try:
            print('    %-26s = %r' % (attr, getattr(pipe, attr)))
        except Exception:
            print('    %-26s = <不存在>' % attr)

    # batch_sampler 里可能藏着 batch_size
    try:
        bs = pipe.batch_sampler
        print('    %-26s = %r' % ('batch_sampler', bs))
        print('    %-26s = %r' % ('  .batch_size',
                                 getattr(bs, 'batch_size', '<无>')))
    except Exception:
        pass
    print()


def run_config(pipe, imgs, predict_kw, batch):
    """跑一组配置，返回耗时与输出规模。"""
    t0 = time.time()
    if batch <= 1:
        outs = []
        for img in imgs:
            outs.extend(list(pipe.predict(img, **predict_kw)))
    else:
        outs = list(pipe.predict(imgs, **predict_kw))
    dt = time.time() - t0
    stat = count_items(outs)
    return {
        'wall_s': round(dt, 1),
        'per_img_s': round(dt / len(imgs), 1),
        'n_results': len(outs),
        **stat,
    }


def test(imgs):
    from paddleocr import PaddleOCRVL

    print('=' * 74)
    print('配置对比测试')
    print('=' * 74)
    print('  图片 %d 张：%s' % (len(imgs), [os.path.basename(x) for x in imgs]))
    print()

    # 每组都要一个干净的 pipeline：配置项（batch_size/use_queues）是构造期参数
    configs = [
        # (名称, 构造参数, predict 参数, 一次喂几张)
        ('baseline', {}, {}, 1),
        # 最有希望：跳过布局检测，整图直接按表格识别。
        # 源码里 use_layout_detection=False 时会把整张图当成一个 block，
        # prompt_label='table' 让它走 "Table Recognition:" 分支，
        # 于是不做段落/页脚/图片说明的识别——我们本来也不要那些。
        ('table_only', {}, {'use_layout_detection': False,
                            'prompt_label': 'table'}, 1),
        ('queues', {'use_queues': True}, {}, 1),
        ('maxtok_1024', {}, {'max_new_tokens': 1024}, 1),
        ('batch4', {'batch_size': 4}, {}, min(len(imgs), 4)),
    ]

    if args.only:
        want = {x.strip() for x in args.only if x.strip()}
        configs = [c for c in configs if c[0] in want]
        if not configs:
            print('  --only 里的名字都不匹配。可选：',
                  ['baseline', 'table_only', 'queues',
                   'maxtok_1024', 'batch4'])
            return
        print('  只跑：%s' % [c[0] for c in configs])

    print('  %-13s %-8s %-7s %-6s %-6s %-6s %-6s %s'
          % ('配置', '总耗时', '单图', '块数', '行数', '项目', '单位', '状态'))
    print('  ' + '-' * 74)

    base = None
    results = []
    for name, init_kw, pred_kw, batch in configs:
        try:
            pipe = PaddleOCRVL(device=DEVICE, **init_kw)
        except Exception as e:
            print('  %-14s 构造失败：%s' % (name, str(e)[:50]))
            results.append({'name': name, 'error': 'init: %s' % str(e)[:120]})
            continue
        try:
            r = run_config(pipe, imgs, pred_kw, batch)
        except Exception as e:
            print('  %-14s 运行失败：%s: %s'
                  % (name, type(e).__name__, str(e)[:50]))
            results.append({'name': name,
                            'error': 'run: %s: %s'
                                     % (type(e).__name__, str(e)[:120])})
            try:
                pipe.close()
            except Exception:
                pass
            continue
        try:
            pipe.close()
        except Exception:
            pass

        if name == 'baseline':
            base = r
            status = '基准'
        elif base:
            speedup = base['wall_s'] / r['wall_s'] if r['wall_s'] else 0
            # 用"抽出项目数"判质量：这是下游真正用的东西。
            # 项目数掉超过 5% 就认为输出受损。
            if r['items'] >= base['items'] * 0.95:
                quality = '完整'
            else:
                quality = '项目↓%d' % (base['items'] - r['items'])
            status = '%.2fx %s' % (speedup, quality)
        else:
            status = '—'
        print('  %-13s %-8.1f %-7.1f %-6d %-6d %-6d %-6d %s'
              % (name, r['wall_s'], r['per_img_s'], r['blocks'],
                 r['table_rows'], r['items'], r['units'], status))
        results.append({'name': name, **r, 'status': status})

    print()
    print('  解读：')
    print('    · 单图耗时下降且"行数"不减 → 该配置可安全采用')
    print('    · 行数明显下降 → 输出被截断，不能用于生产')
    print('    · 若 batch 有效，说明批处理共享权重有收益，比多进程省显存')
    with open(args.out, 'w', encoding='utf-8') as fp:
        json.dump({'images': [os.path.basename(x) for x in imgs],
                   'results': results}, fp, ensure_ascii=False, indent=1)
    print()
    print('  结果写入', args.out)


def main():
    global args
    args = parse_args()
    imgs = list_images(args.images, args.limit)
    if not imgs:
        print('没有图片:', args.images)
        return 1
    if args.mode == 'probe':
        probe()
    else:
        test(imgs)
    return 0


if __name__ == '__main__':
    sys.exit(main())
