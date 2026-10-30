# -*- coding: utf-8 -*-
r"""19 本地：用大模型从 OCR 结果抽取结构化字段（方案B1）

设计：
    输入 = artifacts 里的 HTML 表格（与规则解析器完全相同的输入）
    输出 = predictions.jsonl（与规则解析器完全相同的格式）
    因此两者可用同一个 07_evaluate.py 评测，结果严格可比。

用法：
    # 先试5份看效果和花费
    python tools\19_llm_extract.py --artifacts ..\..\artifacts --limit 5"""

import os
import sys
import json
import time
import glob
import re
import argparse
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding='utf-8')

SEP = '#################'

# few-shot 示例：教模型处理"值-单位-状态粘连"和"双栏"这两种典型情况
FEWSHOT = """示例1（值单位粘连，带状态词）：
表格：
<tr><td>白细胞</td><td>7.1 ( 10^9/L ) 正常</td><td>(3.5-9.5)</td></tr>
<tr><td>淋巴细胞百分比</td><td>26.3 (%) 正常</td><td>(20.0-50.0)</td></tr>
输出：
[{"item_name":"白细胞","item_value":"7.1","item_unit":"10^9/L","item_range":"3.5-9.5"},
 {"item_name":"淋巴细胞百分比","item_value":"26.3","item_unit":"%","item_range":"20.0-50.0"}]

示例2（双栏并排，一行两栏）：
表格：
<tr><td>*WBC</td><td>白细胞绝对值</td><td>8.63</td><td>10E9/L</td><td>3.5 - 9.5</td><td>PDW</td><td>16.4</td></tr>
输出：
[{"item_name":"*WBC","item_value":"8.63","item_unit":"10E9/L","item_range":"3.5 - 9.5"},
 {"item_name":"PDW","item_value":"16.4","item_unit":"","item_range":""}]"""

SYSTEM_PROMPT = """你是医学检验报告的结构化抽取工具。从给定的表格中抽取所有检验项目。

抽取规则：
1. item_name：用表格里的原始名称。有英文缩写（如*WBC、LY#、PDW）就用缩写，没有就用中文全称。
2. item_value：只保留数值本身。去掉单位、去掉"正常/偏高/异常"等状态词、去掉↑↓箭头。
3. item_unit：只保留单位（如10^9/L、%、mmol/L、fL）。
4. item_range：只保留参考范围，去掉外层括号。文本型范围（如Negative、阴性）原样保留。
5. 表格可能是左右双栏并排，一行里有两组项目，都要抽出来。
6. 忽略姓名、性别、科室、病历号、标本号、送检日期、检验者、审核者等非检验项目。

输出要求：只输出JSON数组，不要任何解释、不要markdown代码块。
格式：[{"item_name":"...","item_value":"...","item_unit":"...","item_range":"..."}]"""


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', required=True, help='artifacts目录')
    ap.add_argument('--out', default='predictions_llm.jsonl')
    ap.add_argument('--limit', type=int, default=0, help='最多处理几份，0=全部')
    ap.add_argument('--sleep', type=float, default=0.5, help='每次调用间隔秒数')
    ap.add_argument('--max-tables', type=int, default=6, help='单份最多取几个表格')
    return ap.parse_args()


def get_config():
    base = os.environ.get('LLM_BASE_URL', '').rstrip('/')
    model = os.environ.get('LLM_MODEL', '')
    key = os.environ.get('LLM_API_KEY', '')
    if not (base and model and key):
        print('!! 缺少配置。请先设置环境变量：')
        print('   LLM_BASE_URL  例: https://api.deepseek.com/v1')
        print('   LLM_MODEL     例: deepseek-chat')
        print('   LLM_API_KEY   你的API密钥（不写入文件）')
        sys.exit(1)
    return base, model, key


def call_llm(base, model, key, messages, retries=3, temperature=0.0,
             max_tokens=4000, timeout=120):
    """调用 OpenAI 兼容的 /chat/completions。"""
    url = f'{base}/chat/completions'
    body = json.dumps({
        'model': model,
        'messages': messages,
        'temperature': temperature,  # 抽取用 0.0；解读可给一点余地
        'max_tokens': max_tokens,
    }).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', f'Bearer {key}')

    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return data['choices'][0]['message']['content'], data.get('usage', {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:300]
            last_err = f'HTTP {e.code}: {detail}'
            if e.code == 429:
                # 限流窗口通常按分钟计，等 2、4 秒等于没等。
                # 优先看服务端给的 Retry-After，没有就按指数退避。
                wait = None
                try:
                    ra = e.headers.get('Retry-After')
                    if ra:
                        wait = min(120, int(float(ra)))
                except Exception:
                    wait = None
                if wait is None:
                    wait = min(120, 15 * (2 ** attempt))
                print(f'      限流，{wait} 秒后重试'
                      f'（第 {attempt + 1}/{retries} 次）')
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503):
                time.sleep(2 * (attempt + 1))     # 服务端错误，短退避
                continue
            break
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last_err)


def extract_tables(rec, max_tables):
    """从 artifacts 取 HTML 表格。"""
    tables = []
    for res in (rec.get('results') or []):
        for s in (res.get('parsing_res_list') or []):
            if not isinstance(s, str):
                continue
            for part in s.split(SEP):
                if 'content:' not in part:
                    continue
                for t in re.findall(r'<table[^>]*>.*?</table>', part, re.S | re.I):
                    tables.append(t)
                    if len(tables) >= max_tables:
                        return tables
    return tables


def parse_llm_json(text):
    """从模型回复里抠出 JSON 数组，容忍代码块包裹。"""
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    # 优先整体解析
    try:
        v = json.loads(t)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and 'items' in v:
            return v['items']
    except json.JSONDecodeError:
        pass
    # 退而求其次：找最外层的 [...]
    m = re.search(r'\[.*\]', t, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            if isinstance(v, list):
                return v
        except json.JSONDecodeError:
            pass
    return None


def normalize_items(items):
    """规整成评测脚本期望的字段名。"""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get('item_name') or '').strip()
        if not name:
            continue
        out.append({
            'item_name': name,
            'item_value': str(it.get('item_value') or '').strip(),
            'item_unit': str(it.get('item_unit') or '').strip(),
            'item_range': str(it.get('item_range') or '').strip(),
            'is_abnormal': '',
        })
    return out


def main():
    args = parse_args()
    base, model, key = get_config()
    print(f'模型: {model}')
    print(f'接口: {base}')

    files = sorted(glob.glob(os.path.join(args.artifacts, '*.json')))
    files = [f for f in files if not os.path.basename(f).startswith('_')]
    if args.limit:
        files = files[:args.limit]
    if not files:
        print('artifacts 里没有 json')
        sys.exit(1)
    print(f'待处理: {len(files)} 份\n')

    # 断点续跑：读已有结果
    done = {}
    if os.path.isfile(args.out):
        for line in open(args.out, encoding='utf-8'):
            if line.strip():
                o = json.loads(line)
                done[o['image']] = o
        print(f'已有 {len(done)} 份结果，跳过\n')

    total_in = total_out = 0
    ok = fail = empty = 0

    with open(args.out, 'a', encoding='utf-8') as fo:
        for i, f in enumerate(files, 1):
            rec = json.load(open(f, encoding='utf-8'))
            img = rec.get('image', '')
            if img in done:
                continue

            tables = extract_tables(rec, args.max_tables)
            if not tables:
                print(f'{i:>3} {img[:30]:<32} 无表格，跳过')
                empty += 1
                continue

            user_msg = (FEWSHOT + '\n\n现在请处理下面的表格，只输出JSON数组：\n'
                        + '\n'.join(tables))
            messages = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_msg},
            ]

            t0 = time.time()
            try:
                content, usage = call_llm(base, model, key, messages)
                items = parse_llm_json(content)
                if items is None:
                    raise RuntimeError('模型输出无法解析为JSON数组')
                items = normalize_items(items)
                dt = time.time() - t0
                total_in += usage.get('prompt_tokens', 0)
                total_out += usage.get('completion_tokens', 0)
                line = json.dumps({'image': img, 'items': items}, ensure_ascii=False)
                fo.write(line + '\n')
                fo.flush()
                ok += 1
                print(f'{i:>3} {img[:30]:<32} {len(items):>3}项 {dt:>5.1f}s  '
                      f'gt={rec.get("num_items_gt")}')
            except Exception as e:
                fail += 1
                print(f'{i:>3} {img[:30]:<32} 失败 {type(e).__name__}: {str(e)[:120]}')
            time.sleep(args.sleep)

    print('\n' + '=' * 60)
    print('完成')
    print('=' * 60)
    print(f'成功 {ok}  失败 {fail}  无表格 {empty}')
    print(f'累计 token: 输入 {total_in}  输出 {total_out}')
    # 粗略成本（按 DeepSeek 输入1元/百万、输出2元/百万）
    cost = total_in / 1e6 * 1.0 + total_out / 1e6 * 2.0
    print(f'估算费用: 约 {cost:.3f} 元（按输入1元/M、输出2元/M计）')
    print(f'输出: {args.out}')
    print()
    print('评测（与规则基线同口径）:')
    print(f'  python tools\\07_evaluate.py --labels "<数据目录>/medrepbench/datasets-meta-zhCN.csv" '
          f'--predictions {os.path.basename(args.out)}')


if __name__ == '__main__':
    main()
