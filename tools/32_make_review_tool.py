# -*- coding: utf-8 -*-
r"""32 本地：生成"复核工具"（预填识别结果 + 异常判定，人工只改错和确认）

与 22 / 25 的区别
--------------------
22 从零填空表格；25 预填 OCR 结果；32 在 25 基础上补了四件"量大时才需要"的事：

用法
----
    # 推荐流程：OCR → 解析 → 判异常 → 生成复核工具
    python tools\04_parse_predictions.py --artifacts artifacts --out predictions.jsonl
    python tools\31_judge_abnormal.py --predictions predictions.jsonl ^
        --out predictions_flagged.jsonl
    python tools\32_make_review_tool.py --predictions predictions_flagged.jsonl ^
        --images <图片目录> --out-dir "<数据目录>\标注工具" """

import os
import re
import sys
import json
import glob
import base64
import argparse
import importlib.util
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

MAX_WIDTH_DEFAULT = 2000
IMG_EXT = ('.png', '.jpg', '.jpeg')
# 原图与脱敏图/标注文件的命名常常不一致（'xxx.png' vs 'xxx_masked.png'），
# 匹配前统一剥掉这些后缀，否则一张都配不上。
SUFFIX_RE = re.compile(r'(_masked|_annot|_已脱敏|_脱敏)$', re.I)


def norm_stem(s):
    return SUFFIX_RE.sub('', s or '')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--predictions', default='',
                    help='04 或 31 的输出 jsonl（推荐用 31 的，带 is_abnormal）')
    ap.add_argument('--artifacts', default='',
                    help='兼容旧流程：直接给 OCR artifacts 目录，现解析（无 flag 预填）')
    ap.add_argument('--images', required=True)
    ap.add_argument('--out-dir', default=r'<数据目录>\标注工具')
    ap.add_argument('--resume-from', default='', help='上次导出的 JSON，用来恢复已完成状态')
    ap.add_argument('--keep-flags-from', default='',
                    help='上次导出的 JSON：值用新解析结果，flag 沿用人工标注，'
                         '并标出哪些行的值被修正过（解析器修好后用这个）')
    ap.add_argument('--fix-conflicts', action='store_true',
                    help='把"与参考区间矛盾"的旧标注直接改成按区间的结果，'
                         '并打印改了哪几项（换新值之后旧标注站不住时用）')
    ap.add_argument('--limit', type=int, default=0, help='本批最多几张，0=全部')
    ap.add_argument('--batch', type=int, default=0, help='第几批（从0开始）')
    ap.add_argument('--max-width', type=int, default=MAX_WIDTH_DEFAULT,
                    help='图片压缩到该宽度，0=不压缩')
    ap.add_argument('--out-name', default='', help='输出文件名，默认 review_tool.html')
    return ap.parse_args()


def load_module(fname, modname):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, fname)
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def items_from_predictions(path):
    """返回 {图片名: [item]}，item 里带 is_abnormal / abnormal_reason。"""
    out = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            o = json.loads(line)
            out[o.get('image', '')] = o.get('items') or []
    return out


def items_from_artifacts(artifacts_dir, parser_mod):
    out = {}
    if not os.path.isdir(artifacts_dir) or parser_mod is None:
        return out
    for f in sorted(glob.glob(os.path.join(artifacts_dir, '*.json'))):
        if os.path.basename(f).startswith('_'):
            continue
        try:
            rec = json.load(open(f, encoding='utf-8'))
        except (ValueError, OSError):
            continue
        img = rec.get('image', '')
        if not img:
            continue
        items = []
        try:
            tables = parser_mod.extract_tables(
                parser_mod.get_html_sources(rec))
            for tb in tables:
                for row in tb:
                    items.extend(parser_mod.extract_row_items(row))
        except Exception as e:
            print('  解析 %s 失败: %s' % (img, e))
        out[img] = items
    return out


def nn(s):
    """匹配上次标注用的名字键。

    只统一全半角括号、去标记前缀和空格，**保留括号内容**——
    '红细胞分布宽度 (RDW-SD)' 和 '(RDW-CV)' 去掉括号后键会撞车，
    后一个把前一个覆盖掉，结果误报"值被修正"。
    """
    s = '' if s is None else str(s)
    s = s.replace('（', '(').replace('）', ')')
    s = re.sub(r'^[\s\*＊★☆#＃]+', '', s)
    return re.sub(r'\s+', '', s).lower()


def load_prev(path):
    """读上次导出的标注，返回 {(report_id, 名称键): {flag, value}}。

    用途：解析器修好之后值会变，但人工已经确认过的 flag 不该丢。
    这个索引让工具能"值取新的、flag 用人标的"，并标出哪些行的值被修正过。
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        d = json.load(open(path, encoding='utf-8'))
    except (ValueError, OSError) as e:
        print('  读取上次标注失败: %s' % e)
        return {}
    out = {}
    for s in d.get('samples') or []:
        rid = s.get('report_id') or ''
        for x in s.get('items') or []:
            out[(rid, nn(x.get('name')))] = {
                'flag': (x.get('flag') or '').strip(),
                'value': (x.get('value') or '').strip(),
            }
    n_flag = sum(1 for v in out.values() if v['flag'] in ('normal', 'abnormal'))
    print('  上次标注 %d 项，其中已判定 %d 项' % (len(out), n_flag))
    return out


_J31 = None


def _judge_mod():
    """惰性加载 31_judge_abnormal.py，用于"标注 vs 区间"一致性检查。"""
    global _J31
    if _J31 is None:
        _J31 = load_module('31_judge_abnormal.py', 'j31_for32') or False
    return _J31 or None


def _num_eq(a, b):
    """数值口径相等（'19' == '19.0'），非数值则按字符串比。"""
    def f(x):
        try:
            return float(str(x).strip().lstrip('<>\u2264\u2265\uff1c\uff1e'))
        except (TypeError, ValueError):
            return None
    fa, fb = f(a), f(b)
    if fa is not None and fb is not None:
        return fa == fb
    return str(a or '').strip() == str(b or '').strip()


def to_review_item(it, rid='', prev=None):
    """把预测项转成复核工具要的行结构。

    flag 初始值策略（"预标注"的核心）：
      有人工确认过 → 直接沿用，并标出值是否被修正（值变了旧 flag 就该重看）
      规则高置信   → 填好，人工扫一眼确认即可
      低置信/拒判   → 留空并标色，人工必须自己判断
    """
    prev = prev or {}
    name = (it.get('item_name') or '').strip()
    value = (it.get('item_value') or '').strip()
    flag = (it.get('is_abnormal') or '').strip()
    conf = (it.get('abnormal_confidence') or '').strip()
    reason = (it.get('abnormal_reason') or '').strip()

    old = prev.get((rid, nn(name))) if rid else None
    merged = bool(old and old.get('flag') in ('normal', 'abnormal'))
    # 只有**数值真的变了**才算"值被修正"。
    # '19' vs '19.0'、'0.2' vs '0.20' 只是格式差异，标出来只是噪音。
    changed = bool(merged and not _num_eq(old.get('value'), value))

    # 一致性检查：人工标的 flag 与"按参考区间算出来"的结果矛盾时标出来。
    # 值被解析器修正之后，旧的标注可能就站不住了（'血糖 0.8' 改成 '4.37' 之后
    # 从异常变正常），但人未必会回头重看。这里主动提示，免得 gold 里留下
    # 与数据自相矛盾的标注。
    conflict = False
    auto = ''
    if merged:
        j31 = _judge_mod()
        if j31 is not None:
            v = j31.judge(value, (it.get('item_range') or '').strip())
            auto = v['flag'] if v['flag'] in ('normal', 'abnormal') else ''
            if auto and auto != old['flag']:
                conflict = True

    prefill = old['flag'] if merged else (
        flag if (flag in ('normal', 'abnormal') and conf == 'high') else '')
    return {
        'name': name,
        'value': value,
        'unit': (it.get('item_unit') or '').strip(),
        'range': (it.get('item_range') or '').strip(),
        'flag': prefill,
        'judged': flag,
        'conf': conf,
        'reason': reason,
        # 派生指标会带一句人话说明（'BMI 20.4 正常（18.5-23.9）'）
        'note': (it.get('abnormal_note') or '').strip(),
        # 沿用痕迹：值被修正过的行要人工重看，因为旧 flag 是按旧值判的
        'merged': merged,
        'changed': changed,
        'old_value': (old.get('value') or '') if old else '',
        'conflict': conflict,
        'auto': auto,
    }


def encode_image(path, max_width):
    """读图并可选压缩成 base64。压缩能显著减小 HTML 体积。

    报告单动辄 3000px 宽，30 张原图内嵌就是几百 MB，浏览器打开会卡死。
    降到 1800px 宽对复核够用（要放大看细节时还有界面缩放）。
    """
    if max_width and max_width > 0:
        try:
            from PIL import Image
            import io
            im = Image.open(path)
            if im.width > max_width:
                h = int(im.height * max_width / im.width)
                im = im.convert('RGB').resize((max_width, h), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format='JPEG', quality=92)
            return base64.b64encode(buf.getvalue()).decode('ascii'), 'image/jpeg'
        except Exception as e:
            print('  压缩 %s 失败（用原图）: %s' % (os.path.basename(path), e))
    with open(path, 'rb') as fp:
        data = fp.read()
    ext = os.path.splitext(path)[1].lower()
    mime = 'image/jpeg' if ext in ('.jpg', '.jpeg') else 'image/png'
    return base64.b64encode(data).decode('ascii'), mime


def normalize_resume(sample):
    """把导出格式的样本转成工具内部格式。

    导出时字段叫 range_raw（为的是跟 23_gold_from_annotation 对齐），
    工具内部叫 range。不转的话续标恢复后"参考范围"整列都是空的。
    """
    items = sample.get('items')
    if not isinstance(items, list):
        items = []
    out_items = []
    for x in items:
        out_items.append({
            'name': x.get('name') or '',
            'value': x.get('value') or '',
            'unit': x.get('unit') or '',
            'range': x.get('range_raw') or x.get('range') or '',
            'flag': x.get('flag') or '',
            'judged': x.get('rule_judged') or x.get('judged') or '',
            'conf': x.get('rule_conf') or x.get('conf') or '',
            'reason': x.get('reason') or '',
        })
    return {
        'items': out_items,
        'masks': sample.get('masks') or [],
        '_done': bool(sample.get('_done')),
        '_skip': bool(sample.get('_skip')),
    }


def load_resume(path):
    """读上次导出的 JSON，返回 {report_id: 工具内部格式的样本}。"""
    if not path or not os.path.isfile(path):
        return {}
    try:
        d = json.load(open(path, encoding='utf-8'))
    except (ValueError, OSError) as e:
        print('  读取续标文件失败: %s' % e)
        return {}
    samples = d.get('samples') if isinstance(d, dict) else d
    out = {}
    if isinstance(samples, list):
        for s in samples:
            rid = s.get('report_id') or ''
            if rid:
                out[rid] = normalize_resume(s)
    print('  续标文件里有 %d 份' % len(out))
    return out


def js_json(obj):
    """把数据塞进 <script> 里。

    必须转义 '</'，否则项目名里一旦出现 '</script>' 之类的文本，
    整个页面会在那里被提前截断，而且报错信息完全看不出原因。
    """
    return json.dumps(obj, ensure_ascii=False).replace('</', '<\\/')


HTML_TPL = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>报告单复核工具</title>
<style>
* { box-sizing: border-box; }
body { margin:0; font-family:"Microsoft YaHei",sans-serif; background:#1e1e1e; color:#ddd; display:flex; flex-direction:column; height:100vh; font-size:13px; }
#top { display:flex; align-items:center; gap:10px; padding:6px 10px; background:#2d2d2d; border-bottom:1px solid #444; flex-wrap:wrap; }
#top .sp { flex:1; }
#bar { width:180px; height:8px; background:#3c3c3c; border-radius:4px; overflow:hidden; }
table { width:100%; border-collapse:collapse; }
th { font-size:11px; color:#888; text-align:left; padding:3px 2px; border-bottom:1px solid #444; font-weight:normal; }
td { padding:1px 2px; }
td.hint { font-size:11px; color:#888; white-space:nowrap; }
input { width:100%; font-size:12px; padding:2px 3px; background:#3c3c3c; border:1px solid #555; color:#eee; border-radius:2px; }
select { font-size:12px; padding:2px; background:#3c3c3c; border:1px solid #555; color:#eee; border-radius:2px; width:100%; }
tr.needs input.name { background:#5a3a1a; }
tr.lowconf td { background:#3a3520; }
tr.unknown td { background:#1f2f3a; }
tr.done td { background:#1e3a1e; }
tr.skip { opacity:.45; }
/* 值被解析器修正过的行：旧 flag 是按旧值判的，必须人工重看 */
tr.changed td { background:#3a2a10; }
tr.changed td:first-child { box-shadow: inset 3px 0 0 #d19a3a; }
/* 标注与参考区间矛盾的行：gold 里留下这种会自相矛盾，必须处理 */
tr.conflict td { background:#3d1f1f; }
tr.conflict td:first-child { box-shadow: inset 3px 0 0 #f14c4c; }
.reason { font-size:10px; color:#777; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:110px; display:block; }
.btn { padding:5px 10px; background:#0e639c; border:none; color:#fff; cursor:pointer; font-size:12px; border-radius:3px; }
.btn:hover { background:#1177bb; }
.btn.g { background:#3a6d3a; }
.btn.gray { background:#444; }
.btn.warn { background:#8b2f2f; }
#toast { position:fixed; bottom:14px; right:14px; background:#0e639c; padding:8px 14px; border-radius:4px; font-size:12px; opacity:0; transition:opacity .25s; pointer-events:none; }
#toast.show { opacity:1; }
.hint { font-size:11px; color:#777; line-height:1.7; }
.legend { font-size:11px; color:#999; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 3px 0 8px; vertical-align:middle; }
</style>
</head>
<body>
<div id="top">
  <b>报告单复核</b>
  <span id="counter">-</span>
  <div id="bar"><i></i></div>
  <label class="hint">缩放
    <select id="zoom" onchange="setZoom()" style="width:70px">
      <option value="1">适应</option><option value="1.5">150%</option>
      <option value="2">200%</option><option value="3">300%</option>
    </select>
  </label>
  <span class="sp"></span>
  <input type="file" id="importFile" accept=".json" style="display:none" onchange="importState(this)">
  <button class="btn gray" onclick="document.getElementById('importFile').click()">导入已有标注</button>
  <button class="btn gray" onclick="clearLocal()" title="只清浏览器缓存，导出过的 JSON 不受影响">清空本地缓存</button>
  <button class="btn g" onclick="exportAll()">导出全部标注</button>
</div>
<div style="display:flex">
  <div id="left"><canvas id="cv"></canvas></div>
  <div id="right">
    <div id="tabs"></div>
    <div class="legend">
      <i style="background:#5a3a1a"></i>名称为空
      <i style="background:#3a3520"></i>规则低置信，需人工判
      <i style="background:#1f2f3a"></i>规则拒判，需人工判
      <i style="background:#1e3a1e"></i>已确认
    </div>
    <div style="margin:8px 0">
      <label class="hint"><input type="checkbox" id="onlyTodo" onchange="renderItems()"> 只看待确认</label>
      <label class="hint" style="margin-left:10px"><input type="checkbox" id="onlyAbn" onchange="renderItems()"> 只看已判异常</label>
      <span class="hint" id="rowcount" style="margin-left:10px"></span>
    </div>
    <table id="items">
      <tr>
        <th style="width:4%">#</th>
        <th style="width:27%">项目名称</th>
        <th style="width:14%">结果</th>
        <th style="width:11%">单位</th>
        <th style="width:18%">参考范围</th>
        <th style="width:14%">异常</th>
        <th style="width:5%"></th>
      </tr>
    </table>
    <div style="margin-top:6px">
      <button class="btn gray" onclick="addRow()">+ 添加一行</button>
      <button class="btn gray" onclick="skipSample()">这张跳过</button>
      <button class="btn g" onclick="markDone(true)">确认本张，下一张 →</button>
    </div>
    <div style="margin-top:12px">
      <h3 style="font-size:12px;color:#4ec9b0;margin:6px 0">脱敏（仅当原图未脱敏时用）</h3>
      <div class="hint">在图上拖拽框选姓名/身份证/电话/住址/条形码</div>
      <button class="btn gray" onclick="undoMask()">撤销框</button>
      <button class="btn warn" onclick="clearMask()">清空框</button>
      <span class="hint" id="maskinfo"></span>
      <button class="btn gray" onclick="exportMasked()">导出本张脱敏图</button>
    </div>
    <div class="hint" style="margin-top:12px">
      每敲一次都会自动存到浏览器本地，关掉页面再打开能恢复。<br>
      全部标完后点「导出全部标注」，得到一个 JSON，再跑
      <code>23_gold_from_annotation.py</code> 生成 gold。
    </div>
  </div>
</div>
<div id="toast"></div>
<script>
var IMAGES = __IMAGES__;
var RESUME = __RESUME__;
var STORE_KEY = 'dsh_review_v2';

var cur = 0;
var state = {};
var img = null;
var scale = 1, baseScale = 1, zoom = 1;
var dragging = false, startX = 0, startY = 0, curRect = null;
var saveTimer = null;

function toast(m) {
  var t = document.getElementById('toast');
  t.textContent = m; t.className = 'show';
  setTimeout(function(){ t.className = ''; }, 1500);
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function initState() {
  IMAGES.forEach(function(im) {
    var pre = (im.items || []).map(function(it) { return {
      name: it.name || '', value: it.value || '', unit: it.unit || '',
      range: it.range || '', flag: it.flag || '',
      judged: it.judged || '', conf: it.conf || '', reason: it.reason || '',
      note: it.note || '', merged: !!it.merged, changed: !!it.changed,
      old_value: it.old_value || '', conflict: !!it.conflict, auto: it.auto || ''
    };});
    if (!pre.length) {
      pre.push({name:'',value:'',unit:'',range:'',flag:'',judged:'',conf:'',reason:'',note:''});
    }
    var r = RESUME[im.name];
    state[im.name] = {
      items: (r && Object.prototype.toString.call(r.items) === '[object Array]')
        ? r.items : pre,
      masks: (r && r.masks) || [],
      _done: !!(r && r._done),
      _skip: !!(r && r._skip)
    };
  });
}

function save() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(function() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        saved_at: new Date().toISOString(), state: state
      }));
    } catch (e) { /* 超出配额就算了 */ }
  }, 400);
}

function nn(s) {
  // 与 Python 侧 nn() 同一套规则：统一全半角括号、去标记前缀和空格、保留括号内容。
  // 用于把本地缓存的旧标注按名称合并到新解析结果上。
  s = String(s == null ? '' : s);
  s = s.replace(/（/g, '(').replace(/）/g, ')');
  s = s.replace(/^[\s\*\uff0a\u2605\u2606#\uff03]+/, '');
  return s.replace(/\s+/g, '').toLowerCase();
}

function restore() {
  /* 从浏览器本地缓存恢复。

     这里**不能**整体替换 items。踩过的坑：解析器修好后 100123 从 33 项变 40 项，
     但缓存里存的是旧的 33 项，恢复时整个盖掉，用户按 Ctrl+F5 强刷也没用——
     页面里永远是 33 项。

     正确做法：items 结构以本次生成的新解析结果为准，只把旧标注的 flag
     按名称合并进来。既保住人工已确认的判断，又不会丢新增的项。
  */
  try {
    var raw = localStorage.getItem(STORE_KEY);
    if (!raw) return false;
    var d = JSON.parse(raw);
    if (!d || !d.state) return false;
    var merged = 0, diff = [];
    Object.keys(d.state).forEach(function(k) {
      if (!state[k]) return;
      var old = d.state[k];
      var oldFlag = {};
      (old.items || []).forEach(function(x) {
        var key = nn(x.name);
        if (key) oldFlag[key] = x.flag || '';
      });
      state[k].items.forEach(function(x) {
        var key = nn(x.name);
        if (key && Object.prototype.hasOwnProperty.call(oldFlag, key)) {
          x.flag = oldFlag[key];
          merged++;
        }
      });
      var nOld = (old.items || []).length;
      var nNew = state[k].items.length;
      if (nOld !== nNew) {
        diff.push(k.replace(/^TempDragFile_/, '') + ' ' + nOld + '\u2192' + nNew + ' \u9879');
      }
      state[k].masks = old.masks || state[k].masks;
      state[k]._done = !!old._done;
      state[k]._skip = !!old._skip;
    });
    if (diff.length) {
      toast('\u89e3\u6790\u7ed3\u679c\u5df2\u66f4\u65b0\uff1a' + diff.join('\uff1b'));
    } else if (merged) {
      toast('\u5df2\u6062\u590d\u4e0a\u6b21\u8fdb\u5ea6\uff08' + new Date(d.saved_at).toLocaleString() + '\uff09');
    }
    return merged > 0;
  } catch (e) { return false; }
}

function clearLocal() {
  if (!confirm('\u6e05\u7a7a\u6d4f\u89c8\u5668\u91cc\u4fdd\u5b58\u7684\u6807\u6ce8\u8fdb\u5ea6\uff1f\n\u5bfc\u51fa\u7684 JSON \u4e0d\u53d7\u5f71\u54cd\u3002')) return;
  try { localStorage.removeItem(STORE_KEY); } catch (e) {}
  location.reload();
}

function counter() {
  var done = 0, total = IMAGES.length;
  IMAGES.forEach(function(im) { if (state[im.name]._done) done++; });
  document.getElementById('counter').textContent = done + ' / ' + total + ' 已确认';
  document.getElementById('bar').firstElementChild.style.width =
    (total ? (done / total * 100) : 0) + '%';
}

function buildTabs() {
  var tabs = document.getElementById('tabs');
  tabs.innerHTML = '';
  IMAGES.forEach(function(im, i) {
    var b = document.createElement('button');
    b.textContent = im.short;
    if (i === cur) b.className = 'active';
    if (state[im.name]._done) b.className += ' done';
    b.onclick = function() { cur = i; loadImage(); };
    tabs.appendChild(b);
  });
}

function loadImage() {
  buildTabs();
  counter();
  // 表格和脱敏信息必须在这里就刷新：它们只依赖 state，不依赖图片加载完成。
  // 之前漏了这两行，表现是"切换样本时只有图片变了、右侧表格还是上一份"。
  renderItems();
  maskInfo();
  img = new Image();
  img.onload = function() {
    var cv = document.getElementById('cv');
    cv.width = img.width; cv.height = img.height;
    var box = document.getElementById('left');
    baseScale = Math.min(1,
      (box.clientWidth - 30) / img.width,
      (box.clientHeight - 30) / img.height);
    setZoom();
  };
  img.src = IMAGES[cur].mime_data;
}

function setZoom() {
  zoom = parseFloat(document.getElementById('zoom').value || '1');
  scale = baseScale * zoom;
  var cv = document.getElementById('cv');
  if (img) {
    cv.style.width = (img.width * scale) + 'px';
    cv.style.height = (img.height * scale) + 'px';
  }
  redraw();
}

function redraw() {
  var cv = document.getElementById('cv');
  var ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (img) ctx.drawImage(img, 0, 0);
  ctx.fillStyle = '#000';
  state[IMAGES[cur].name].masks.forEach(function(m) {
    ctx.fillRect(m.x, m.y, m.w, m.h);
  });
  if (curRect) {
    ctx.strokeStyle = '#ff5555'; ctx.lineWidth = 3;
    ctx.strokeRect(curRect.x, curRect.y, curRect.w, curRect.h);
  }
}

function evPos(e) {
  var cv = document.getElementById('cv');
  var r = cv.getBoundingClientRect();
  return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale };
}

document.addEventListener('DOMContentLoaded', function() {
  var cv = document.getElementById('cv');
  cv.addEventListener('mousedown', function(e) {
    var p = evPos(e); dragging = true; startX = p.x; startY = p.y;
    curRect = {x: p.x, y: p.y, w: 0, h: 0};
  });
  cv.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var p = evPos(e);
    curRect = {
      x: Math.min(startX, p.x), y: Math.min(startY, p.y),
      w: Math.abs(p.x - startX), h: Math.abs(p.y - startY)
    };
    redraw();
  });
  cv.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    if (curRect && curRect.w > 4 && curRect.h > 4) {
      state[IMAGES[cur].name].masks.push(curRect);
      save();
    }
    curRect = null; redraw(); maskInfo();
  });
});

function rowClass(it, tr) {
  var cls = [];
  if (it.conflict) cls.push('conflict');
  else if (it.changed) cls.push('changed');
  if (!String(it.name || '').trim()) cls.push('needs');
  if (it.flag === 'normal' || it.flag === 'abnormal') {
    if (it.judged && it.flag === it.judged && it.conf === 'high') cls.push('done');
  } else if (it.conf === 'low') {
    cls.push('lowconf');
  } else if (!it.judged) {
    cls.push('unknown');
  }
  if (state[IMAGES[cur].name]._skip) cls.push('skip');
  return cls.join(' ');
}

// 规则原因 → 中文。原始 reason（如 no_rule）直接显示没人看得懂
var REASON_CN = {
  'in_range': '区间内', 'above_high': '高于上限', 'below_low': '低于下限',
  'above_upper': '高于上限', 'under_upper': '未超上限',
  'below_lower': '低于下限', 'over_lower_ambiguous': '>阈值·存疑',
  'sign_word': '定性词', 'sign_word_over_interval': '定性词',
  'arrow_up': '报告标↑', 'arrow_down': '报告标↓',
  'no_rule': '无参考区间', 'text_reference': '参考非数值',
  'qualitative_needs_threshold': '缺阈值', 'no_value': '无结果',
  'value_not_numeric': '结果非数值', 'at_upper_strict': '等于上限',
  'derived_bmi': 'BMI 派生'
};
function reasonCN(r) { return REASON_CN[r] || (r || ''); }

function renderItems() {
  var tbl = document.getElementById('items');
  while (tbl.rows.length > 1) tbl.deleteRow(1);
  var items = state[IMAGES[cur].name].items;
  var onlyTodo = document.getElementById('onlyTodo').checked;
  var onlyAbn = document.getElementById('onlyAbn').checked;
  var shown = 0, abn = 0;
  items.forEach(function(it, i) {
    var confirmed = (it.flag === 'normal' || it.flag === 'abnormal');
    if (it.flag === 'abnormal') abn++;
    if (onlyTodo && confirmed) return;
    if (onlyAbn && it.flag !== 'abnormal') return;
    shown++;
    var tr = tbl.insertRow();
    tr.className = rowClass(it, tr);
    tr.innerHTML =
      '<td class="hint" title="' +
        esc(it.changed
              ? '值已修正：' + (it.old_value || '空') + ' → ' + (it.value || '空')
              : '') + '">' +
        (i + 1) + (it.changed ? ' ↻' : '') + '</td>' +
      '<td><input class="name" value="' + esc(it.name) + '" oninput="setItem(' + i + ',\'name\',this.value)"></td>' +
      '<td><input value="' + esc(it.value) + '" oninput="setItem(' + i + ',\'value\',this.value)"></td>' +
      '<td><input value="' + esc(it.unit) + '" oninput="setItem(' + i + ',\'unit\',this.value)"></td>' +
      '<td><input value="' + esc(it.range) + '" oninput="setItem(' + i + ',\'range\',this.value)"></td>' +
      '<td><select onchange="setItem(' + i + ',\'flag\',this.value)">' +
        '<option value=""' + (it.flag ? '' : ' selected') + '>未判</option>' +
        '<option value="normal"' + (it.flag === 'normal' ? ' selected' : '') + '>正常</option>' +
        '<option value="abnormal"' + (it.flag === 'abnormal' ? ' selected' : '') + '>异常</option>' +
      '</select><span class="reason" title="' + esc(it.note || reasonCN(it.reason)) + '">' +
        esc(it.conflict
              ? '⚠ 与区间矛盾，应' + (it.auto === 'abnormal' ? '异常' : '正常')
              : (it.note || reasonCN(it.reason))) + '</span></td>' +
      '<td><button class="btn gray" style="padding:1px 6px" onclick="delRow(' + i + ')">×</button></td>';
  });
  var rc = document.getElementById('rowcount');
  if (rc) rc.textContent = '显示 ' + shown + ' / ' + items.length +
    ' 行，其中判异常 ' + abn + ' 项';
}

function setItem(i, k, v) {
  state[IMAGES[cur].name].items[i][k] = v;
  if (k === 'flag' || k === 'name') {
    var tbl = document.getElementById('items');
    renderItems();
  }
  save();
}
function addRow() {
  state[IMAGES[cur].name].items.push(
    {name:'',value:'',unit:'',range:'',flag:'',judged:'',conf:'',reason:'',note:'',
     merged:false,changed:false,old_value:'',conflict:false,auto:''});
  renderItems(); save();
}
function delRow(i) {
  state[IMAGES[cur].name].items.splice(i, 1);
  renderItems(); save();
}

function undoMask() {
  var m = state[IMAGES[cur].name].masks;
  if (m.length) { m.pop(); redraw(); maskInfo(); save(); }
}
function clearMask() {
  state[IMAGES[cur].name].masks = []; redraw(); maskInfo(); save();
}
function maskInfo() {
  document.getElementById('maskinfo').textContent =
    '已框选 ' + state[IMAGES[cur].name].masks.length + ' 处';
}

function markDone(advance) {
  state[IMAGES[cur].name]._done = true;
  save(); buildTabs(); counter(); renderItems();
  toast('已确认');
  if (advance) {
    setTimeout(function() {
      if (cur < IMAGES.length - 1) { cur++; loadImage(); }
    }, 300);
  }
}
function skipSample() {
  state[IMAGES[cur].name]._skip = true;
  save(); renderItems();
  toast('已标记跳过');
}

function download(name, blob) {
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

function exportAll() {
  var samples = [];
  var totalItems = 0, confirmed = 0;
  IMAGES.forEach(function(im) {
    var st = state[im.name];
    var items = st.items.filter(function(x) {
      return String(x.name || '').trim();
    }).map(function(x) {
      totalItems++;
      if (x.flag === 'normal' || x.flag === 'abnormal') confirmed++;
      return {
        name: String(x.name).trim(),
        value: String(x.value || '').trim(),
        unit: String(x.unit || '').trim(),
        range_raw: String(x.range || '').trim(),
        flag: (x.flag === 'normal' || x.flag === 'abnormal') ? x.flag : '',
        rule_judged: x.judged || '',
        rule_conf: x.conf || ''
      };
    });
    samples.push({
      report_id: im.stem,
      source_image: im.name,
      masked_image: im.stem + '_masked.png',
      masks: st.masks,
      _done: !!st._done,
      _skip: !!st._skip,
      items: items
    });
  });
  var doc = {
    version: 2,
    exported_at: new Date().toISOString(),
    note: '复核工具导出。flag 为空表示人工未判定；rule_judged/rule_conf 是规则引擎的预填值，仅供追溯。',
    samples: samples
  };
  var ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  download('review_export_' + ts + '.json',
    new Blob([JSON.stringify(doc, null, 2)], {type: 'application/json'}));
  toast('已导出 ' + samples.length + ' 份，' + totalItems + ' 项（已判异常 ' + confirmed + ' 项）');
}

function exportMasked() {
  var st = state[IMAGES[cur].name];
  var out = document.createElement('canvas');
  out.width = img.width; out.height = img.height;
  var o = out.getContext('2d');
  o.drawImage(img, 0, 0);
  o.fillStyle = '#000';
  st.masks.forEach(function(m) { o.fillRect(m.x, m.y, m.w, m.h); });
  out.toBlob(function(b) {
    download(IMAGES[cur].stem + '_masked.png', b);
  }, 'image/png');
}

function importState(input) {
  var f = input.files && input.files[0];
  if (!f) return;
  var r = new FileReader();
  r.onload = function() {
    try {
      var d = JSON.parse(r.result);
      var samples = d.samples || d;
      var n = 0;
      samples.forEach(function(s) {
        var key = s.source_image || '';
        if (!state[key] && s.report_id) {
          var hit = IMAGES.filter(function(im) { return im.stem === s.report_id; })[0];
          if (hit) key = hit.name;
        }
        if (state[key]) {
          state[key].items = (s.items || []).map(function(x) {
            return {
              name: x.name || '', value: x.value || '', unit: x.unit || '',
              range: x.range_raw || x.range || '',
              flag: x.flag || '',
              judged: x.rule_judged || x.judged || '',
              conf: x.rule_conf || x.conf || '',
              reason: x.reason || '',
              note: x.note || '', merged:false, changed:false, old_value:'',
              conflict:false, auto:''
            };
          });
          if (!state[key].items.length) {
            state[key].items = [{name:'',value:'',unit:'',range:'',
              flag:'',judged:'',conf:'',reason:'',note:'',
              merged:false,changed:false,old_value:'',conflict:false,auto:''}];
          }
          state[key].masks = s.masks || [];
          state[key]._done = !!s._done;
          state[key]._skip = !!s._skip;
          n++;
        }
      });
      save(); renderItems(); buildTabs(); counter();
      toast('已导入 ' + n + ' 份');
    } catch (e) {
      toast('导入失败：' + e.message);
    }
  };
  r.readAsText(f);
  input.value = '';
}

initState();
restore();
loadImage();
renderItems();
maskInfo();
</script>
</body>
</html>
'''


def main():
    args = parse_args()

    if args.predictions:
        src = items_from_predictions(args.predictions)
        print('预测文件: %s  样本 %d' % (args.predictions, len(src)))
    elif args.artifacts:
        parser_mod = load_module('04_parse_predictions.py', 'p04_for32')
        if parser_mod is None:
            print('!! 找不到 04_parse_predictions.py')
            sys.exit(1)
        src = items_from_artifacts(args.artifacts, parser_mod)
        print('artifacts: %s  样本 %d（无 flag 预填）' % (args.artifacts, len(src)))
    else:
        print('需要 --predictions 或 --artifacts')
        sys.exit(1)

    imgs = sorted([f for f in glob.glob(os.path.join(args.images, '*'))
                   if os.path.isfile(f) and f.lower().endswith(IMG_EXT)])
    if not imgs:
        print('%s 下没有图片' % args.images)
        sys.exit(1)

    # 按文件名（忽略扩展名、忽略 _masked 等后缀）匹配预测
    stem_map = {}
    for img_name, items in src.items():
        stem_map[norm_stem(os.path.splitext(os.path.basename(img_name))[0])] = items

    seen = {}
    all_imgs = []
    for f in imgs:
        name = os.path.basename(f)
        stem = os.path.splitext(name)[0]
        key = norm_stem(stem)
        # 同一份报告既有原图又有脱敏图时，只保留一张（优先已脱敏的）
        if key in seen:
            if '_masked' in name.lower() and '_masked' not in seen[key][1].lower():
                all_imgs[seen[key][0]] = (f, name, stem, stem_map.get(key, []))
                seen[key] = (seen[key][0], name)
            continue
        seen[key] = (len(all_imgs), name)
        all_imgs.append((f, name, stem, stem_map.get(key, [])))

    matched = sum(1 for _, _, _, it in all_imgs if it)
    print('图片 %d 张，其中 %d 张匹配到识别结果' % (len(all_imgs), matched))
    if matched == 0:
        print()
        print('!! 一张都没匹配上。检查预测文件里的 image 字段与图片文件名是否一致。')
        print('   预测里的 image 示例:', list(src.keys())[:3])
        print('   图片目录里的名字示例:', [n for _, n, _, _ in all_imgs][:3])
        sys.exit(1)

    # 分批
    if args.limit and args.limit > 0:
        start = args.batch * args.limit
        batch_imgs = all_imgs[start:start + args.limit]
        if not batch_imgs:
            print('第 %d 批为空（总共 %d 张，每批 %d）'
                  % (args.batch, len(all_imgs), args.limit))
            sys.exit(1)
        print('分批：第 %d 批，取第 %d-%d 张，共 %d 张'
              % (args.batch, start + 1, start + len(batch_imgs), len(batch_imgs)))
    else:
        batch_imgs = all_imgs

    resume = load_resume(args.resume_from)
    prev_flags = load_prev(args.keep_flags_from)

    images_payload = []
    resume_payload = {}
    n_merged = n_changed = 0
    fixed = []
    for f, name, stem, items in batch_imgs:
        b64, mime = encode_image(f, args.max_width)
        ritems = []
        for it in items:
            ri = to_review_item(it, stem, prev_flags)
            n_merged += 1 if ri['merged'] else 0
            n_changed += 1 if ri['changed'] else 0
            if args.fix_conflicts and ri['conflict']:
                fixed.append((stem, ri['name'], ri['value'], ri['range'],
                              ri['flag'], ri['auto']))
                ri['flag'] = ri['auto']
                ri['conflict'] = False
            ritems.append(ri)
        images_payload.append({
            'name': name,
            'stem': stem,
            'short': re.sub(r'^TempDragFile_', '', stem),
            'mime_data': 'data:%s;base64,%s' % (mime, b64),
            'items': ritems,
        })
        if stem in resume:
            resume_payload[name] = resume[stem]

    html = HTML_TPL.replace('__IMAGES__', js_json(images_payload))
    html = html.replace('__RESUME__', js_json(resume_payload))

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, 'output'), exist_ok=True)
    out_name = args.out_name or 'review_tool.html'
    out = os.path.join(args.out_dir, out_name)
    with open(out, 'w', encoding='utf-8') as fp:
        fp.write(html)

    n_items = sum(len(p['items']) for p in images_payload)
    n_prefilled = sum(1 for p in images_payload for it in p['items']
                      if it['flag'] in ('normal', 'abnormal'))
    n_lowconf = sum(1 for p in images_payload for it in p['items']
                    if it['conf'] == 'low')
    n_unknown = sum(1 for p in images_payload for it in p['items']
                    if not it['judged'])

    print()
    print('=' * 60)
    print('复核工具已生成')
    print('=' * 60)
    print('  %s  (%.1f MB)' % (out, os.path.getsize(out) / 1024 / 1024))
    print('  样本 %d 份，项目 %d 个' % (len(images_payload), n_items))
    print('  异常栏已预填 %d 个（规则高置信，扫一眼确认即可）' % n_prefilled)
    print('  需人工判 %d 个（低置信 %d + 拒判 %d）' % (n_lowconf + n_unknown,
                                                n_lowconf, n_unknown))
    if n_merged:
        print('  沿用上次人工标注 %d 个 flag' % n_merged)
    if n_changed:
        print('  %d 个项的值被解析器修正过（行号旁有 ↻，悬停可看新旧值）' % n_changed)
    if fixed:
        print()
        print('  已按参考区间自动修正 %d 项与区间矛盾的旧标注：' % len(fixed))
        for stem, nm, val, rng, was, now in fixed:
            print('    %-20s %-22s 值=%-8s 区间=%-12s %s → %s'
                  % (stem.replace('TempDragFile_', '')[:18], nm, val, rng, was, now))
    if resume_payload:
        print('  从续标文件恢复了 %d 份' % len(resume_payload))
    print()
    print('用法：')
    print('  1. 浏览器打开，橙色/黄色/蓝色的行是重点')
    print('  2. 改错、补漏、确认「异常」栏')
    print('  3. 每敲一次自动存本地，关掉再开能恢复')
    print('  4. 全部标完点「导出全部标注」→ 一个 JSON')
    print('  5. python tools\\23_gold_from_annotation.py --src <下载目录> '
          '--out <数据目录>\\自采数据集')


if __name__ == '__main__':
    main()
