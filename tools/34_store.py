# -*- coding: utf-8 -*-
r"""34 本地：数据落库原型（SQLite）——为"前后对比"和"引用补全"定数据契约

为什么现在就要定
----------------
报告单会越攒越多，之后要做的两件事都依赖同一套底层结构：
  · 前后对比：同一受检者多次报告的指标趋势
  · 引用补全：本次报告缺项时，引用最近一次的有效值（如身高体重）

用法
----
    # 建库 + 导入解析结果
    python tools\34_store.py --init lab.db
    python tools\34_store.py --db lab.db --import predictions_self_flagged.jsonl ^
        --subject SAMPLE --institution "示范医院" """

import os
import re
import sys
import json
import hmac
import hashlib
import sqlite3
import argparse
from datetime import date, datetime

sys.stdout.reconfigure(encoding='utf-8')

SCHEMA_VERSION = '1'

DDL = """
PRAGMA journal_mode=WAL;

-- 受检者（只放脱敏后的稳定标识）
CREATE TABLE IF NOT EXISTS subject (
    subject_id   TEXT PRIMARY KEY,
    sex          TEXT,
    birth_year   INTEGER,
    note         TEXT,
    created_at   TEXT NOT NULL
);

-- 报告
CREATE TABLE IF NOT EXISTS report (
    report_id      TEXT PRIMARY KEY,
    subject_id     TEXT,              -- 可空：匿名样本没有受检者标识
    report_date    TEXT,              -- ISO 日期，可空：脱敏常把日期涂掉
    institution    TEXT,
    source_image   TEXT,
    schema_version TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (subject_id) REFERENCES subject(subject_id)
);

-- 指标明细
CREATE TABLE IF NOT EXISTS item (
    report_id        TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    name_raw         TEXT NOT NULL,
    name_key         TEXT NOT NULL,   -- 归一化名，跨报告对齐用
    indicator_code   TEXT,
    value_raw        TEXT,
    value_num        REAL,            -- 能转成数值时才有，用于趋势与区间比对
    unit             TEXT,
    ref_raw          TEXT,
    ref_kind         TEXT,            -- interval/one_sided_high/one_sided_low/text/missing
    ref_low          REAL,
    ref_high         REAL,
    flag             TEXT,            -- normal / abnormal / unknown
    flag_reason      TEXT,
    flag_confidence  TEXT,
    flag_note        TEXT,
    -- 值来源：measured=本次实测，carried=引用历史（必须配 source_report_id）
    value_source     TEXT NOT NULL DEFAULT 'measured',
    source_report_id TEXT,
    PRIMARY KEY (report_id, seq),
    FOREIGN KEY (report_id) REFERENCES report(report_id)
);

CREATE INDEX IF NOT EXISTS idx_item_name    ON item(name_key);
CREATE INDEX IF NOT EXISTS idx_item_code    ON item(indicator_code);
CREATE INDEX IF NOT EXISTS idx_report_subj  ON report(subject_id, report_date);

-- 解读：一份报告可以有多条解读记录（换模型 / 换提示词后重生成），
-- 取最新一条展示，但旧的保留，便于回答"上一版解读错在哪"
CREATE TABLE IF NOT EXISTS explanation (
    explanation_id TEXT PRIMARY KEY,
    report_id      TEXT NOT NULL,
    tier           TEXT NOT NULL,     -- llm=模型生成 / template=模板降级
    model          TEXT,
    prompt_version TEXT,
    guard_status   TEXT NOT NULL,     -- pass / degraded / blocked
    guard_detail   TEXT,              -- JSON：命中了哪些规则
    n_items        INTEGER,
    n_abnormal     INTEGER,
    n_unknown      INTEGER,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (report_id) REFERENCES report(report_id)
);

CREATE INDEX IF NOT EXISTS idx_exp_report ON explanation(report_id, created_at);

-- 解读段落拆成行而不是整段存 JSON：
-- 每个段落要能反查它引用了哪一项指标，出错的解读必须定位得到具体数值
CREATE TABLE IF NOT EXISTS explanation_section (
    explanation_id TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    kind           TEXT NOT NULL,     -- status / abnormal / unknown / advice
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    -- 这一段提到的指标（name_key 逗号分隔）。模型输出里没有结构化引用，
    -- 只能由正文匹配还原，所以是一对多，且可能为空
    cite_items     TEXT,
    PRIMARY KEY (explanation_id, seq),
    FOREIGN KEY (explanation_id) REFERENCES explanation(explanation_id)
);

CREATE INDEX IF NOT EXISTS idx_expsec_cite ON explanation_section(cite_items);
"""

NUM_RE = re.compile(r'^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='lab.db')
    ap.add_argument('--init', action='store_true', help='只建表')
    ap.add_argument('--import', dest='import_path', default='',
                    help='导入 predictions jsonl')
    ap.add_argument('--subject', default='', help='受检者标识（见 make_subject_id）')
    ap.add_argument('--institution', default='')
    ap.add_argument('--report-date', default='', help='YYYY-MM-DD，留空则从数据里取')
    ap.add_argument('--history', default='', help='查某项目的历史序列')
    ap.add_argument('--carry', default='', help='列出某报告的引用补全建议')
    ap.add_argument('--max-age-days', type=int, default=180)
    ap.add_argument('--salt', default='', help='生成 subject_id 用；或用环境变量 LAB_SUBJECT_SALT')
    ap.add_argument('--selftest', action='store_true', help='验证跨报告能力，不碰真实库')
    return ap.parse_args()


# ---------- 标识脱敏 ----------
def make_subject_id(raw, salt):
    """把"姓名+身份证后四位"这类可识别信息转成稳定且不可还原的标识。

    salt 必须保密且每套部署不同。换 salt 等于换一批标识，历史数据就对不上了，
    所以 salt 要跟数据库一起备份。
    """
    if not raw:
        return ''
    key = (salt or '').encode('utf-8')
    msg = raw.strip().encode('utf-8')
    return 's_' + hmac.new(key, msg, hashlib.sha256).hexdigest()[:24]


# ---------- 归一化 ----------
def name_key(name):
    """跨报告对齐用的键。与 07_evaluate.py 的 normalize_text 保持同一套规则。"""
    s = '' if name is None else str(name)
    s = s.strip().lower().replace(' ', '')
    for a, b in (('：', ':'), ('－', '-'), ('—', '-'), ('–', '-'),
                 ('~', '-'), ('～', '-'), ('（', '('), ('）', ')')):
        s = s.replace(a, b)
    s = re.sub(r'^[\*＊★☆#＃]+', '', s)
    return s


def to_num(v):
    s = '' if v is None else str(v).strip()
    if not s:
        return None
    s = re.sub(r'[↑↓]', '', s).lstrip('<>≤≥＜＞')
    if NUM_RE.match(s):
        try:
            return float(s)
        except ValueError:
            return None
    return None


def clean_ref(raw):
    """参考范围文本清洗，只为展示和落库统一，不改变语义。

    OCR 常把 4.4-11.9 认成 4.4--11.9，直接展示给用户像笔误；
    拆区间用的是 ref_bounds，它自己认得双横线，但存进 ref_raw 的必须是干净的。
    比较符号统一成半角，比较符前后不留空格。
    """
    s = '' if raw is None else str(raw).strip()
    for a, b in (('＜', '<'), ('＞', '>'), ('＝', '='),
                 ('～', '-'), ('~', '-'), ('—', '-'), ('–', '-')):
        s = s.replace(a, b)
    s = re.sub(r'-{2,}', '-', s)
    s = re.sub(r'\s*-\s*', '-', s)
    s = re.sub(r'\s*([<>≤≥])\s*', r'\1', s)
    return s.strip()


def ref_bounds(raw):
    """把 item_range 文本解析成 (kind, low, high)，只做落库需要的粗解析。"""
    s = '' if raw is None else str(raw).strip()
    if not s:
        return 'missing', None, None
    t = s.replace(' ', '').replace('＜', '<').replace('＞', '>')
    m = re.match(r'^([-+]?\d+(?:\.\d+)?)(?:--|—|–|~|～|-)'
                 r'([-+]?\d+(?:\.\d+)?)', t)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return 'interval', lo, hi
    m = re.match(r'^([<>≤≥])([-+]?\d+(?:\.\d+)?)', t)
    if m:
        v = float(m.group(2))
        return ('one_sided_high' if m.group(1) in '<≤' else 'one_sided_low',
                None if m.group(1) in '<≤' else v,
                v if m.group(1) in '<≤' else None)
    return 'text', None, None


# ---------- 建库与写入 ----------
def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def init_db(path):
    conn = connect(path)
    conn.close()
    return path


def upsert_report(conn, rep, subject_id='', institution='', report_date=''):
    """落一份报告。rep 是 predictions.jsonl 的一行。"""
    now = datetime.now().isoformat(timespec='seconds')
    rid = rep.get('report_id') or rep.get('image') or ''
    if not rid:
        raise ValueError('报告缺少 report_id / image')
    rid = os.path.splitext(os.path.basename(rid))[0]
    # 预测里的 image 常带 _masked 后缀，报告号不该带，
    # 否则同一份报告在原图/脱敏图两条路径下会变成两条记录
    rid = re.sub(r'(_masked|_annot|_已脱敏|_脱敏)$', '', rid, flags=re.I)
    rdate = report_date or (rep.get('report_date') or '')
    if subject_id:
        conn.execute(
            'INSERT OR IGNORE INTO subject(subject_id, created_at) VALUES (?,?)',
            (subject_id, now))
    conn.execute(
        """INSERT INTO report(report_id, subject_id, report_date, institution,
                              source_image, schema_version, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(report_id) DO UPDATE SET
             subject_id=excluded.subject_id,
             report_date=excluded.report_date,
             institution=excluded.institution,
             source_image=excluded.source_image,
             created_at=excluded.created_at""",
        (rid, subject_id or None, rdate or None, institution or None,
         rep.get('image') or '', SCHEMA_VERSION, now))
    conn.execute('DELETE FROM item WHERE report_id=?', (rid,))

    n = 0
    for i, it in enumerate(rep.get('items') or []):
        name = (it.get('item_name') or '').strip()
        if not name:
            continue
        rng = clean_ref(it.get('item_range'))
        kind, lo, hi = ref_bounds(rng)
        conn.execute(
            """INSERT INTO item(report_id, seq, name_raw, name_key, indicator_code,
                                value_raw, value_num, unit, ref_raw, ref_kind,
                                ref_low, ref_high, flag, flag_reason,
                                flag_confidence, flag_note, value_source,
                                source_report_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'measured',NULL)""",
            (rid, n,
             name, name_key(name), (it.get('item_code') or None),
             (it.get('item_value') or ''), to_num(it.get('item_value')),
             (it.get('item_unit') or ''), rng, kind,
             lo, hi, (it.get('is_abnormal') or ''), (it.get('abnormal_reason') or ''),
             (it.get('abnormal_confidence') or ''), (it.get('abnormal_note') or '')))
        n += 1
    conn.commit()
    return rid, n


# ---------- 查询：趋势 ----------
def item_history(conn, key, subject_id='', limit=50):
    """某项目的历史序列，按报告日期升序（没有日期的排在最后）。"""
    k = name_key(key)
    sql = """SELECT r.report_id, r.report_date, i.name_raw, i.value_raw,
                    i.value_num, i.unit, i.ref_raw, i.flag, i.value_source,
                    i.source_report_id
             FROM item i JOIN report r ON r.report_id = i.report_id
             WHERE (i.name_key = ? OR i.indicator_code = ?)"""
    params = [k, k]
    if subject_id:
        sql += ' AND r.subject_id = ?'
        params.append(subject_id)
    sql += ' ORDER BY (r.report_date IS NULL), r.report_date, r.report_id LIMIT ?'
    params.append(limit)
    return [dict(x) for x in conn.execute(sql, params)]


def latest_value(conn, key, subject_id, before_date, max_age_days=180):
    """取最近一次**实测**值，供引用补全参考。

    返回 dict(value_num, value_raw, unit, source_report_id, source_date,
              age_days) 或 None。age_days 超过 max_age_days 时返回 None——
    半年前的体重不该拿来算这次 BMI。
    """
    if not subject_id:
        return None
    k = name_key(key)
    sql = """SELECT i.value_num, i.value_raw, i.unit, i.report_id,
                    r.report_date
             FROM item i JOIN report r ON r.report_id = i.report_id
             WHERE (i.name_key = ? OR i.indicator_code = ?)
               AND r.subject_id = ?
               AND i.value_num IS NOT NULL
               AND i.value_source = 'measured'
               AND r.report_date IS NOT NULL"""
    params = [k, k, subject_id]
    if before_date:
        sql += ' AND r.report_date <= ?'
        params.append(before_date)
    sql += ' ORDER BY r.report_date DESC, i.seq LIMIT 1'
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    try:
        d1 = date.fromisoformat(row['report_date'])
        d0 = date.fromisoformat(before_date) if before_date else date.today()
        age = (d0 - d1).days
    except ValueError:
        age = None
    if age is not None and age > max_age_days:
        return None
    return {'value_num': row['value_num'], 'value_raw': row['value_raw'],
            'unit': row['unit'], 'source_report_id': row['report_id'],
            'source_date': row['report_date'], 'age_days': age}


# 这些项目缺了可以引用历史值（本身就是人的基本参数，短期不变或变化缓慢）
CARRYABLE = ('身高', '体重', '腰围', '臀围', '年龄')


def carry_forward(conn, report_id, max_age_days=180):
    """为某报告生成"引用补全"建议。**只出建议，不写库**。

    每条建议都带来源，调用方采用后必须把 value_source 改成 'carried'
    并填 source_report_id。界面上必须显示来源日期。
    """
    rep = conn.execute('SELECT * FROM report WHERE report_id=?',
                       (report_id,)).fetchone()
    if not rep:
        return []
    present = {name_key(x['name_key']) for x in conn.execute(
        'SELECT name_key FROM item WHERE report_id=?', (report_id,))}
    out = []
    for want in CARRYABLE:
        if name_key(want) in present:
            continue
        hit = latest_value(conn, want, rep['subject_id'],
                           rep['report_date'], max_age_days)
        if hit:
            out.append({
                'name': want,
                'value': hit['value_raw'],
                'value_num': hit['value_num'],
                'unit': hit['unit'],
                'source_report_id': hit['source_report_id'],
                'source_date': hit['source_date'],
                'age_days': hit['age_days'],
                'display': '%s %s（%s 测得）' % (hit['value_raw'],
                                                hit['unit'] or '',
                                                hit['source_date']),
            })
    return out


def pending_for_judge(conn, report_id):
    """参与异常判定的项：只取本次实测值。

    引用补全的值不参与判定——这是我们刻意在接口层面挡住的，
    免得后面有人在别处图省事把 carried 值也塞进判定。
    """
    return [dict(x) for x in conn.execute(
        """SELECT * FROM item WHERE report_id=? AND value_source='measured'
           ORDER BY seq""", (report_id,))]


def selftest():
    """验证跨报告能力：趋势查询、引用补全（带来源）、超期不引用、判定只吃实测值。"""
    import tempfile
    bad = []

    def check(cond, msg, detail=''):
        print('%s %s%s' % ('[ OK ]' if cond else '[FAIL]', msg,
                           '' if cond or not detail else '  → %s' % detail))
        if not cond:
            bad.append(msg)

    def rep(rid, d, items):
        return {'report_id': rid, 'report_date': d,
                'items': [{'item_name': n, 'item_value': v, 'item_unit': u,
                           'item_range': r, 'is_abnormal': f}
                          for n, v, u, r, f in items]}

    tmp = os.path.join(tempfile.mkdtemp(prefix='store_selftest_'), 't.db')
    conn = None
    try:
        conn = connect(tmp)
        subj = make_subject_id('张三|3301', 'demo-salt')
        # 第一次体检：有身高体重
        upsert_report(conn, rep('R1', '2026-01-10', [
            ('身高', '176', 'cm', '', ''),
            ('体重', '78', 'kg', '', ''),
            ('甘油三酯', '2.10', 'mmol/L', '0.00~1.70', 'abnormal'),
        ]), subj, '示范医院')
        # 第二次体检：没测身高体重（与 R1 相隔 151 天，在时效内）
        upsert_report(conn, rep('R2', '2026-06-10', [
            ('甘油三酯', '1.65', 'mmol/L', '0.00~1.70', 'normal'),
        ]), subj, '示范医院')
        # 第三次：隔了两年多，超出时效
        upsert_report(conn, rep('R3', '2028-07-10', [
            ('甘油三酯', '1.80', 'mmol/L', '0.00~1.70', 'abnormal'),
        ]), subj, '示范医院')

        check(make_subject_id('张三|3301', 'demo-salt') == subj,
              'subject_id 稳定可复现')
        check(make_subject_id('张三|3301', 'other-salt') != subj,
              '换 salt 得到不同标识（salt 必须随库备份）')
        check('张三' not in subj, 'subject_id 里不含明文身份信息')

        h = item_history(conn, '甘油三酯', subj)
        check(len(h) == 3, '趋势查询取到 3 条记录', )
        check([x['report_date'] for x in h] == ['2026-01-10', '2026-06-10',
                                                '2028-07-10'],
              '趋势按日期升序', str([x['report_date'] for x in h]))
        vals = [x['value_num'] for x in h]
        check(vals == [2.1, 1.65, 1.8], '趋势数值可比较（甘油三酯在降）', str(vals))

        check(item_history(conn, '不存在的项目') == [], '查不到时返回空')

        sug = carry_forward(conn, 'R2', max_age_days=180)
        names = [s['name'] for s in sug]
        check('身高' in names and '体重' in names,
              'R2 缺身高体重，能给出引用建议', str(names))
        s0 = [s for s in sug if s['name'] == '身高'][0]
        check(s0['source_report_id'] == 'R1', '建议带来源报告号', s0['source_report_id'])
        check(s0['source_date'] == '2026-01-10', '建议带来源日期', s0['source_date'])
        check('2026-01-10 测得' in s0['display'], '显示串里写明来源日期', s0['display'])
        check(s0['age_days'] == 151, '给出距今天数', str(s0['age_days']))

        check(carry_forward(conn, 'R1') == [] or
              all(s['name'] not in ('身高', '体重')
                  for s in carry_forward(conn, 'R1')),
              'R1 自己已有身高体重，不重复建议')

        sug3 = carry_forward(conn, 'R3', max_age_days=180)
        check(all(s['name'] not in ('身高', '体重') for s in sug3),
              '超过 180 天的历史值不引用', str([s['name'] for s in sug3]))

        # carried 值不参与判定
        conn.execute("UPDATE item SET value_source='carried', source_report_id='R1' "
                     "WHERE report_id='R2' AND name_key='甘油三酯'")
        conn.commit()
        pend = [x['name_key'] for x in pending_for_judge(conn, 'R2')]
        check('甘油三酯' not in pend,
              'pending_for_judge 排除 carried 值（引用值不参与异常判定）', str(pend))

        # 匿名报告：没有 subject_id 就不做引用
        upsert_report(conn, rep('R9', '2026-07-10', [('甘油三酯', '1.7', 'mmol/L',
                                                     '0.00~1.70', 'normal')]), '')
        check(carry_forward(conn, 'R9') == [],
              '没有 subject_id 的报告不做引用补全')
    finally:
        if conn is not None:
            conn.close()
        for suffix in ('', '-wal', '-shm'):
            p = tmp + suffix
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        d = os.path.dirname(tmp)
        if os.path.isdir(d) and not os.listdir(d):
            try:
                os.rmdir(d)
            except OSError:
                pass

    print()
    print('=' * 58)
    if bad:
        print('失败 %d 项' % len(bad))
        return 1
    print('全部通过')
    return 0


def main():
    args = parse_args()
    if args.selftest:
        return selftest()
    if args.init:
        init_db(args.db)
        print('已建表:', args.db)
        print('  tables: subject / report / item')
        return 0

    if not os.path.isfile(args.db) and not args.init:
        print('数据库不存在，先跑 --init')
        return 1
    conn = connect(args.db)

    if args.import_path:
        salt = args.salt or os.environ.get('LAB_SUBJECT_SALT', '')
        subj = ''
        if args.subject:
            subj = make_subject_id(args.subject, salt) if salt else args.subject
            if not salt:
                print('!! 没有 --salt 或 LAB_SUBJECT_SALT，subject_id 用的是明文输入，')
                print('   正式数据必须加盐哈希。当前只适合本地测试。')
        rows = [json.loads(l) for l in open(args.import_path, encoding='utf-8')
                if l.strip()]
        total = 0
        for rep in rows:
            rid, n = upsert_report(conn, rep, subj, args.institution,
                                   args.report_date)
            total += n
            print('  %-46s %3d 项' % (rid[:44], n))
        print()
        print('导入 %d 份报告，%d 个项目' % (len(rows), total))
        print('  subject_id:', subj or '(空，匿名样本无法做前后对比)')

    if args.history:
        rows = item_history(conn, args.history)
        if not rows:
            print('没有 %r 的历史记录' % args.history)
        else:
            print('%-24s %-12s %-10s %-10s %-14s %s'
                  % ('名称', '日期', '结果', '单位', '参考范围', '判定'))
            for r in rows:
                print('%-24s %-12s %-10s %-10s %-14s %s'
                      % ((r['name_raw'] or '')[:22], r['report_date'] or '-',
                         r['value_raw'] or '', r['unit'] or '',
                         r['ref_raw'] or '', r['flag'] or ''))

    if args.carry:
        sug = carry_forward(conn, args.carry, args.max_age_days)
        rep = conn.execute('SELECT * FROM report WHERE report_id=?',
                           (args.carry,)).fetchone()
        if not rep:
            print('没有这份报告:', args.carry)
        elif not rep['subject_id']:
            print('这份报告没有 subject_id，无法做引用补全')
            print('  前后对比和引用补全都要求能识别"是不是同一个人"')
        elif not sug:
            print('没有可引用的缺失项（或历史值超出 %d 天）' % args.max_age_days)
        else:
            print('引用补全建议（只出建议，采用时须写 value_source=carried）:')
            for s in sug:
                print('  %-8s ← %s' % (s['name'], s['display']))
                print('            来源 %s，%s 天前'
                      % (s['source_report_id'], s['age_days']))
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
