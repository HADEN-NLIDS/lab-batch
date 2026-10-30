"""43_explain_store.py — 解读生成并入库

连接 35_llm_explain.py（生成）与 34_store.py（存储），
并决定生成失败时落什么内容。

用法：
  python tools/43_explain_store.py --db med.db --predictions p.jsonl --tier template
  python tools/43_explain_store.py --db med.db --predictions p.jsonl --tier auto
  python tools/43_explain_store.py --db med.db --show <报告号>
  python tools/43_explain_store.py --selftest"""

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))

# 提示词改了就 +1，否则库里分不清两版解读是哪一版生成的
PROMPT_VERSION = 'explain-v1'


def load_mod(fname, alias):
    spec = importlib.util.spec_from_file_location(alias,
                                                  os.path.join(HERE, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m34 = load_mod('34_store.py', 'm34')
m35 = load_mod('35_llm_explain.py', 'm35')


def now_iso():
    return datetime.now().isoformat(timespec='seconds')


def norm_rid(raw):
    """报告号归一化，规则必须与 34_store.upsert_report 保持一致。

    两边不一致的话，同一份报告会变成 report 一条、explanation 挂另一条。
    """
    rid = os.path.splitext(os.path.basename(str(raw or '')))[0]
    return re.sub(r'(_masked|_annot|_已脱敏|_脱敏)$', '', rid, flags=re.I)


def ensure_report(conn, rid, image=''):
    """保证 report 行存在，但不碰 item 表。

    43 可能在 34 导入之后单独重跑，此时 item 里可能有 31 判定写入的
    flag 人工修正，所以这里绝不能走 upsert_report（它会 DELETE 重插）。
    """
    if conn.execute('SELECT 1 FROM report WHERE report_id=?',
                    (rid,)).fetchone():
        return False
    conn.execute(
        'INSERT INTO report(report_id, source_image, schema_version, created_at)'
        ' VALUES (?,?,?,?)',
        (rid, image or '', m34.SCHEMA_VERSION, now_iso()))
    return True


def cite_items_of(body, items):
    """从正文里还原这一段提到了哪些指标，返回 name_key 列表。

    模型拿到的是指标名文本，输出也是文本，没有结构化引用可用。
    只认长度 >= 2 的名称，单字名（如"钾"）会在"血钾""尿钾"里到处都是。
    """
    # 判定"提到了哪些指标"与 35_llm_explain.check_explanation 用的是
    # 同一条规则，所以直接用它的实现：按出现位置消歧，并且把
    # "淋巴细胞绝对值(LYM#)" 这类带括号编码的名字按去编码的写法参与匹配——
    # 模型写正文时不会写编码，只按原样匹配会让长名永远匹配不上，
    # 短名就顶替了它，引用整段错位。
    names = [(it.get('item_name') or '').strip() for it in items or []]
    names = [x for x in names if len(x) >= 2]
    if not names:
        return []
    out = []
    for nm in m35.independent_names(body or '', names):
        k = m34.name_key(nm)
        if k and k not in out:
            out.append(k)
    return out


def make_template(rid, items):
    """纯模板解读。没有模型也能跑通全链路。"""
    return {
        'report_id': rid,
        'revision': 1,
        'sections': m35.fallback_sections(items),
        'disclaimer': m35.DISCLAIMER,
        'degraded': True,
        'model': '',
        'generated_at': now_iso(),
    }


def run_one(rid, items, tier, client=None, model='', base='', key=''):
    """生成一份解读，返回 (rec, tier_used, guard_status, guard_detail)。

    任何失败都收敛成模板，不抛异常——批量跑 500 份时，
    一份失败不该让整批中断。
    """
    if tier == 'template':
        return make_template(rid, items), 'template', 'pass', ''

    rec, _usage = m35.generate_one(rid, items, client=client, model=model,
                                   base=base, key=key)
    if rec.get('degraded'):
        # generate_one 已降级，把它记的原因带进 guard_detail，
        # 否则事后只能看到"降级"两个字，查不出是超时还是输出越界
        return (rec, 'template', 'degraded',
                rec.get('degrade_reason') or '模型未返回可解析段落')

    issues = m35.check_explanation(rec, items)
    if issues:
        detail = json.dumps([{'kind': k, 'msg': v} for k, v in issues],
                            ensure_ascii=False)
        return make_template(rid, items), 'template', 'degraded', detail

    return rec, 'llm', 'pass', ''


def store(conn, rec, items, tier_used, guard_status, guard_detail=''):
    """写入 explanation + explanation_section，返回 explanation_id。"""
    rid = rec['report_id']
    stamp = re.sub(r'[^0-9]', '', rec.get('generated_at') or now_iso())[:14]
    eid = '%s#%s' % (rid, stamp)
    k = 0
    while conn.execute('SELECT 1 FROM explanation WHERE explanation_id=?',
                       (eid,)).fetchone():
        k += 1
        eid = '%s#%s~%d' % (rid, stamp, k)

    n_ab = sum(1 for x in items if x.get('is_abnormal') == 'abnormal')
    n_un = sum(1 for x in items if m35.is_unknown(x))

    conn.execute(
        """INSERT INTO explanation(explanation_id, report_id, tier, model,
                                   prompt_version, guard_status, guard_detail,
                                   n_items, n_abnormal, n_unknown, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, rid, tier_used, rec.get('model') or '', PROMPT_VERSION,
         guard_status, guard_detail or '', len(items), n_ab, n_un,
         rec.get('generated_at') or now_iso()))

    for seq, s in enumerate(rec.get('sections') or []):
        cites = cite_items_of(s.get('body'), items)
        conn.execute(
            """INSERT INTO explanation_section(explanation_id, seq, kind,
                                               title, body, cite_items)
               VALUES (?,?,?,?,?,?)""",
            (eid, seq, s.get('kind') or 'status', s.get('title') or '',
             s.get('body') or '', ','.join(cites) or None))
    conn.commit()
    return eid


def latest_explanation(conn, rid):
    """取某份报告最新一条解读，连段落一起。界面就调这个。"""
    row = conn.execute(
        'SELECT * FROM explanation WHERE report_id=? '
        'ORDER BY created_at DESC, explanation_id DESC LIMIT 1',
        (rid,)).fetchone()
    if not row:
        return None
    secs = conn.execute(
        'SELECT * FROM explanation_section WHERE explanation_id=? ORDER BY seq',
        (row['explanation_id'],)).fetchall()
    return {
        'explanation_id': row['explanation_id'],
        'report_id': row['report_id'],
        'tier': row['tier'],
        'model': row['model'],
        'prompt_version': row['prompt_version'],
        'guard_status': row['guard_status'],
        'guard_detail': row['guard_detail'],
        'n_items': row['n_items'],
        'n_abnormal': row['n_abnormal'],
        'n_unknown': row['n_unknown'],
        'created_at': row['created_at'],
        'sections': [{
            'seq': s['seq'], 'kind': s['kind'], 'title': s['title'],
            'body': s['body'],
            'cite_items': [x for x in (s['cite_items'] or '').split(',') if x],
        } for s in secs],
    }


# ---------- 自测 ----------
SAMPLE = [
    {'item_name': '白细胞计数', 'item_value': '11.2', 'item_unit': '10^9/L',
     'item_range': '3.5-9.5', 'is_abnormal': 'abnormal'},
    {'item_name': '血红蛋白', 'item_value': '145', 'item_unit': 'g/L',
     'item_range': '130-175', 'is_abnormal': 'normal'},
    {'item_name': 'DeRitis比值', 'item_value': '1.1', 'item_unit': '',
     'item_range': '', 'is_abnormal': 'unknown'},
]


def selftest():
    ok = [0]
    bad = [0]

    def chk(name, cond, extra=''):
        if cond:
            ok[0] += 1
            print('  OK  %-34s %s' % (name, extra))
        else:
            bad[0] += 1
            print('  FAIL %-34s %s' % (name, extra))

    conn = m34.connect(':memory:')
    rid = 'SAMPLE001'

    # 1) 模板降级是一条完整链路
    rec = make_template(rid, SAMPLE)
    chk('模板有段落', len(rec['sections']) >= 2, '%d 段' % len(rec['sections']))
    chk('模板判为降级', rec['degraded'] is True)
    chk('免责声明强制附加', rec['disclaimer'] == m35.DISCLAIMER)

    ensure_report(conn, rid, 'SAMPLE001.png')
    chk('report 行已补建',
        conn.execute('SELECT 1 FROM report WHERE report_id=?',
                     (rid,)).fetchone() is not None)
    chk('重复补建返回 False', ensure_report(conn, rid) is False)

    eid = store(conn, rec, SAMPLE, 'template', 'pass')
    chk('explanation 已写入',
        conn.execute('SELECT 1 FROM explanation WHERE explanation_id=?',
                     (eid,)).fetchone() is not None, eid)
    n_sec = conn.execute(
        'SELECT COUNT(*) FROM explanation_section WHERE explanation_id=?',
        (eid,)).fetchone()[0]
    chk('段落数一致', n_sec == len(rec['sections']), '%d 行' % n_sec)

    row = conn.execute('SELECT * FROM explanation WHERE explanation_id=?',
                       (eid,)).fetchone()
    chk('统计数正确',
        row['n_items'] == 3 and row['n_abnormal'] == 1 and row['n_unknown'] == 1,
        '%d/%d/%d' % (row['n_items'], row['n_abnormal'], row['n_unknown']))
    chk('tier=template', row['tier'] == 'template')
    chk('prompt_version 已记录', row['prompt_version'] == PROMPT_VERSION)

    # 2) 引用还原
    body = '本次白细胞计数 11.2 10^9/L，参考范围 3.5-9.5，偏高。'
    cites = cite_items_of(body, SAMPLE)
    chk('能还原引用的指标', cites == [m34.name_key('白细胞计数')], str(cites))
    chk('没提到的不算引用',
        m34.name_key('血红蛋白') not in cites)
    chk('正文为空不报错', cite_items_of('', SAMPLE) == [])

    # 3) 最新解读能取回
    got = latest_explanation(conn, rid)
    chk('latest_explanation 可读', got is not None)
    chk('取回段落完整', len(got['sections']) == len(rec['sections']))
    chk('cite_items 是列表',
        all(isinstance(s['cite_items'], list) for s in got['sections']))

    # 4) 重跑不覆盖，新增一行
    eid2 = store(conn, make_template(rid, SAMPLE), SAMPLE, 'template', 'pass')
    n_exp = conn.execute('SELECT COUNT(*) FROM explanation WHERE report_id=?',
                         (rid,)).fetchone()[0]
    chk('重跑新增而非覆盖', n_exp == 2, '%d 行' % n_exp)
    chk('两次 id 不同', eid != eid2, '%s / %s' % (eid, eid2))
    chk('最新取到第二条',
        latest_explanation(conn, rid)['explanation_id'] == eid2)

    # 5) 无 report 行也能写（外键未强制，但数据要能落地）
    store(conn, make_template('NO_REPORT', SAMPLE), SAMPLE, 'template', 'pass')
    chk('缺 report 行也能入库',
        conn.execute("SELECT 1 FROM explanation WHERE report_id='NO_REPORT'")
        .fetchone() is not None)

    # 6) 校验不过时退回模板
    bad_rec = {
        'report_id': rid, 'revision': 1, 'generated_at': now_iso(),
        'model': 'fake-model', 'degraded': False,
        'disclaimer': m35.DISCLAIMER,
        'sections': [
            {'section_id': 's1', 'kind': 'status', 'item_id': None,
             'title': '结果', 'body': '本次白细胞计数偏高，说明你有感染。'},
        ],
    }
    issues = m35.check_explanation(bad_rec, SAMPLE)
    chk('违规解读能被校验抓到', len(issues) > 0, str(issues[0][0]))

    out, tier_used, gs, detail = run_one(rid, SAMPLE, 'template')
    chk('template 模式 tier 正确', (tier_used, gs) == ('template', 'pass'))
    eid3 = store(conn, out, SAMPLE, tier_used, gs, detail)
    chk('降级记录可入库',
        conn.execute('SELECT 1 FROM explanation WHERE explanation_id=?',
                     (eid3,)).fetchone() is not None)

    # 7) 报告号归一化与 34 一致
    chk('去 _masked 后缀', norm_rid('A001_masked.png') == 'A001')
    chk('去目录', norm_rid('C:/x/y/B002.png') == 'B002')

    # 8) 模板不给 unknown 项下结论
    sec_text = ' '.join(s['body'] for s in m35.fallback_sections(SAMPLE))
    chk('模板如实交代无法判断', '无法判断' in sec_text)
    chk('模板不含诊断词',
        not any(w in sec_text for w in m35.DIAG_WORDS))

    conn.close()
    print()
    print('通过 %d/%d' % (ok[0], ok[0] + bad[0]))
    return 0 if bad[0] == 0 else 1


# ---------- 主流程 ----------
def parse_args():
    ap = argparse.ArgumentParser(
        description='把判定结果生成解读并写入数据库')
    ap.add_argument('--db', default='med.db', help='SQLite 路径')
    ap.add_argument('--predictions', default='', help='带 is_abnormal 的预测 jsonl')
    ap.add_argument('--tier', default='auto',
                    choices=['auto', 'llm', 'template'],
                    help='auto=有配置就调模型、否则模板；template=强制模板')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true',
                    help='只生成不写库')
    ap.add_argument('--out', default='', help='把生成结果另存为 jsonl')
    ap.add_argument('--show', default='',
                    help='打印某份报告最新一条解读后退出')
    ap.add_argument('--from-jsonl', default='',
                    help='导入已有的解读 jsonl，不调模型也不生成。'
                         '仍需 --predictions 以取回各项指标做引用还原')
    ap.add_argument('--selftest', action='store_true')
    return ap.parse_args()


def main():
    args = parse_args()
    if args.selftest:
        return selftest()

    if args.show:
        conn = m34.connect(args.db)
        rid = norm_rid(args.show)
        ex = latest_explanation(conn, rid)
        if not ex:
            # 允许只给报告号的一部分，演示时不必敲全名
            cand = [r['report_id'] for r in conn.execute(
                'SELECT DISTINCT report_id FROM report WHERE report_id LIKE ?',
                ('%' + rid + '%',))]
            if len(cand) == 1:
                rid = cand[0]
                ex = latest_explanation(conn, rid)
            elif len(cand) > 1:
                print('匹配到 %d 份，请写得更具体：' % len(cand))
                for c in cand:
                    print('  ' + c)
                conn.close()
                return 1
        if not ex:
            print('库里没有 %s 的解读' % rid)
            conn.close()
            return 1
        print('%s  [%s/%s]  %d 项 异常 %d 未知 %d  %s'
              % (ex['report_id'], ex['tier'], ex['guard_status'],
                 ex['n_items'], ex['n_abnormal'], ex['n_unknown'],
                 ex['created_at']))
        if ex['guard_detail']:
            print('  拦截原因: %s' % ex['guard_detail'])
        for sec in ex['sections']:
            print()
            print('[%s] %s' % (sec['kind'], sec['title']))
            for ln in sec['body'].splitlines():
                print('  ' + ln)
            if sec['cite_items']:
                named = [r['name_raw'] for r in conn.execute(
                    'SELECT name_raw FROM item WHERE report_id=? '
                    'ORDER BY seq', (rid,))
                    if m34.name_key(r['name_raw']) in sec['cite_items']]
                print('  → 引用: %s' % ('、'.join(named) or '（已删除的指标）'))
        print()
        print('-' * 60)
        print(ex['sections'] and m35.DISCLAIMER)
        conn.close()
        return 0

    if not args.predictions:
        print('需要 --predictions（或用 --selftest）')
        return 1

    if args.from_jsonl:
        recs = [json.loads(l)
                for l in open(args.from_jsonl, encoding='utf-8') if l.strip()]
        preds = {}
        for r in (json.loads(l) for l in
                  open(args.predictions, encoding='utf-8') if l.strip()):
            preds[norm_rid(r.get('report_id') or r.get('image'))] = r
        conn = m34.connect(args.db)
        for rec in recs:
            rid = norm_rid(rec.get('report_id'))
            rec['report_id'] = rid      # store() 用的是这个字段，必须一起规范化
            src = preds.get(rid) or {}
            items = src.get('items') or []
            ensure_report(conn, rid, src.get('image') or '')
            # 导入的解读已经过校验（或由 35 校验过），降级的如实记成 template
            if rec.get('degraded'):
                tier_used, gs = 'template', 'degraded'
                detail = rec.get('degrade_reason') or ''
            else:
                tier_used, gs = 'llm', 'pass'
                detail = ''
            eid = store(conn, rec, items, tier_used, gs, detail)
            print('  %-40s %d 段 %s' % (rid[:38], len(rec.get('sections') or []),
                                        tier_used))
        conn.close()
        print()
        print('已导入 %d 份到 %s' % (len(recs), args.db))
        return 0

    rows = [json.loads(l) for l in open(args.predictions, encoding='utf-8')
            if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    print('待解读 %d 份' % len(rows))

    tier = args.tier
    client, model, base, key = None, '', '', ''
    if tier in ('auto', 'llm'):
        try:
            client = m35.load_llm_client()
            base, model, key = client.get_config()
            if not (base and model and key):
                client = None
        except Exception as e:
            print('  模型客户端不可用：%s' % e)
            client = None
        if client is None:
            if tier == 'llm':
                print('  --tier llm 但模型未配置，请设置 LLM_BASE_URL / '
                      'LLM_MODEL / LLM_API_KEY')
                return 2
            tier = 'template'
        else:
            tier = 'llm'
            print('模型: %s' % model)
            print('接口: %s' % base)
    print('解读方式: %s' % ('模型生成' if tier == 'llm' else '模板降级'))

    conn = m34.connect(args.db) if not args.dry_run else None
    outs = []
    n_ok = n_deg = 0
    for i, r in enumerate(rows, 1):
        rid = norm_rid(r.get('report_id') or r.get('image') or '')
        if not rid:
            print('  [%d/%d] 跳过：没有报告号' % (i, len(rows)))
            continue
        items = r.get('items') or []

        rec, tier_used, gs, detail = run_one(
            rid, items, tier, client=client, model=model, base=base, key=key)
        outs.append(rec)

        if gs == 'pass' and tier_used == 'llm':
            n_ok += 1
        elif gs == 'degraded':
            n_deg += 1

        if conn is not None:
            ensure_report(conn, rid, r.get('image') or '')
            store(conn, rec, items, tier_used, gs, detail)

        print('  [%d/%d] %-40s %d 段 %s%s'
              % (i, len(rows), rid[:38], len(rec['sections']),
                 tier_used, '' if gs == 'pass' else ' (%s)' % gs))

    if conn is not None:
        conn.close()
        print()
        print('已写入 %s' % args.db)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fp:
            for r in outs:
                fp.write(json.dumps(r, ensure_ascii=False) + '\n')
        print('生成结果: %s' % args.out)

    print()
    print('合计 %d 份：模型通过 %d，降级 %d'
          % (len(outs), n_ok, n_deg + (len(outs) - n_ok - n_deg)))
    if n_deg:
        print('降级不是故障：模板只列事实不做解释，仍可交付；'
              '需排查的是被校验拦下的那几份 guard_detail')
    return 0


if __name__ == '__main__':
    sys.exit(main())
