# -*- coding: utf-8 -*-
r"""22 本地：生成脱敏+标注工具（单文件HTML，浏览器直接打开）

为什么做成HTML而不是GUI：
    · 零依赖（不需要tkinter/opencv）
    · 框选脱敏、填表标注可在同一页完成
    · 导出即为脱敏图 + 标注JSON

用法：
    python tools\22_make_annotator.py
    （图片默认从 <图片目录> 读取）"""

import os
import sys
import glob
import json
import base64
import argparse

sys.stdout.reconfigure(encoding='utf-8')


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--src', default=r'<图片目录>')
    ap.add_argument('--out-dir', default=r'<数据目录>\标注工具')
    return ap.parse_args()


HTML_TMPL = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>检验单脱敏与标注工具</title>
<style>
* { box-sizing: border-box; }
body { margin:0; font-family: "Microsoft YaHei", sans-serif; background:#1e1e1e; color:#ddd; display:flex; flex-direction:column; height:100vh; }
#tabs { display:flex; background:#2d2d2d; border-bottom:1px solid #444; flex-wrap:wrap; }
#tabs button { padding:8px 14px; background:none; border:none; color:#aaa; cursor:pointer; font-size:13px; border-bottom:2px solid transparent; }
#tabs button.active { color:#4ec9b0; border-bottom-color:#4ec9b0; background:#252526; }
h3 { margin:6px 0 10px; font-size:14px; color:#4ec9b0; }
label { display:block; font-size:12px; color:#999; margin:8px 0 3px; }
input[type=text] { width:100%; padding:5px; background:#3c3c3c; border:1px solid #555; color:#eee; font-size:13px; }
#items { width:100%; border-collapse:collapse; margin-top:8px; }
#items th { font-size:11px; color:#888; text-align:left; padding:3px; border-bottom:1px solid #444; }
#items td { padding:2px; }
#items input { font-size:12px; padding:3px; }
.btn { padding:6px 12px; background:#0e639c; border:none; color:#fff; cursor:pointer; font-size:13px; border-radius:3px; margin:4px 4px 0 0; }
.btn:hover { background:#1177bb; }
.btn.warn { background:#8b2f2f; }
.btn.gray { background:#444; }
#status { position:fixed; bottom:10px; right:10px; background:#0e639c; padding:6px 12px; border-radius:4px; font-size:12px; opacity:0; transition:opacity .3s; }
#status.show { opacity:1; }
.hint { font-size:11px; color:#777; line-height:1.6; margin-top:10px; }
</style>
</head>
<body>
<div id="tabs"></div>
<div id="main">
  <div id="left"><canvas id="cv"></canvas></div>
  <div id="right">
    <h3>脱敏</h3>
    <label>在左侧图片上拖拽框选要打码的区域（可多次框选）</label>
    <button class="btn gray" onclick="undoMask()">撤销上一个框</button>
    <button class="btn warn" onclick="clearMask()">清空所有框</button>
    <div class="hint" id="maskinfo">已框选：0 处</div>

    <h3 style="margin-top:20px">标注检验项目</h3>
    <table id="items">
      <tr><th style="width:34%">项目名称</th><th style="width:22%">结果</th><th style="width:18%">单位</th><th style="width:26%">参考范围</th><th></th></tr>
    </table>
    <button class="btn gray" onclick="addRow()">+ 添加一行</button>

    <h3 style="margin-top:20px">导出</h3>
    <button class="btn" onclick="exportAll()">保存本张（脱敏图+JSON）</button>
    <button class="btn" onclick="exportAll()">保存并下一张 →</button>

    <div class="hint">
      操作说明：<br>
      1. 拖拽框选个人信息区域（姓名/身份证/电话/住址/条形码等）<br>
      2. 在右侧逐项填写检验项目（参考范围填原文，如 3.5-9.5 或 9.4--12.5）<br>
      3. 点"保存"，浏览器会下载两个文件<br>
      4. 把下载的文件放进 School\标注工具\output\ 目录<br>
      5. 参考范围可留空（原图没有就留空）
    </div>
  </div>
</div>
<div id="status"></div>

<script>
const IMAGES = __IMAGES__;

let cur = 0;
const state = {};   // name -> {masks:[{x,y,w,h}], items:[{name,value,unit,range}]}
IMAGES.forEach(im => { state[im.name] = {masks:[], items:[{name:'',value:'',unit:'',range:''}]}; });

const tabs = document.getElementById('tabs');
const cv = document.getElementById('cv');
const ctx = cv.getContext('2d');
let img = new Image();
let scale = 1;
let dragging = false, startX = 0, startY = 0, curRect = null;

function toast(msg) {
  const s = document.getElementById('status');
  s.textContent = msg; s.classList.add('show');
  setTimeout(()=>s.classList.remove('show'), 1800);
}

function buildTabs() {
  tabs.innerHTML = '';
  IMAGES.forEach((im, i) => {
    const b = document.createElement('button');
    b.textContent = im.name.replace(/^TempDragFile_/, '').replace(/\.png$/, '');
    if (i === cur) b.classList.add('active');
    if (state[im.name].masks.length || state[im.name].items[0].name) b.classList.add('done');
    b.onclick = () => { cur = i; loadImage(); };
    tabs.appendChild(b);
  });
}

function loadImage() {
  buildTabs();
  img = new Image();
  img.onload = () => {
    cv.width = img.width; cv.height = img.height;
    // 显示尺寸适配左侧容器
    const box = document.getElementById('left');
    const maxW = box.clientWidth - 24;
    const maxH = box.clientHeight - 24;
    const s = Math.min(1, maxW / img.width, maxH / img.height);
    cv.style.width = (img.width * s) + 'px';
    cv.style.height = (img.height * s) + 'px';
    scale = s;
    redraw();
    renderItems();
    updateMaskInfo();
  };
  img.src = 'data:image/png;base64,' + IMAGES[cur].b64;
}

function redraw() {
  ctx.clearRect(0,0,cv.width,cv.height);
  ctx.drawImage(img, 0, 0);
  const st = state[IMAGES[cur].name];
  ctx.fillStyle = '#000';
  st.masks.forEach(m => ctx.fillRect(m.x, m.y, m.w, m.h));
  if (curRect) {
    ctx.strokeStyle = '#ff5555'; ctx.lineWidth = 3;
    ctx.strokeRect(curRect.x, curRect.y, curRect.w, curRect.h);
  }
}

function evPos(e) {
  const r = cv.getBoundingClientRect();
  return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale };
}

cv.addEventListener('mousedown', e => {
  const p = evPos(e); dragging = true; startX = p.x; startY = p.y; curRect = {x:p.x,y:p.y,w:0,h:0};
});
cv.addEventListener('mousemove', e => {
  if (!dragging) return;
  const p = evPos(e);
  curRect = {x: Math.min(startX,p.x), y: Math.min(startY,p.y),
             w: Math.abs(p.x-startX), h: Math.abs(p.y-startY)};
  redraw();
});
cv.addEventListener('mouseup', e => {
  if (!dragging) return;
  dragging = false;
  if (curRect && curRect.w > 4 && curRect.h > 4) {
    state[IMAGES[cur].name].masks.push(curRect);
  }
  curRect = null;
  redraw(); updateMaskInfo(); buildTabs();
});

function undoMask() {
  const m = state[IMAGES[cur].name].masks;
  if (m.length) { m.pop(); redraw(); updateMaskInfo(); buildTabs(); toast('已撤销'); }
}
function clearMask() {
  state[IMAGES[cur].name].masks = [];
  redraw(); updateMaskInfo(); buildTabs(); toast('已清空');
}
function updateMaskInfo() {
  document.getElementById('maskinfo').textContent =
    '已框选：' + state[IMAGES[cur].name].masks.length + ' 处';
}

function renderItems() {
  const tbl = document.getElementById('items');
  while (tbl.rows.length > 1) tbl.deleteRow(1);
  const items = state[IMAGES[cur].name].items;
  items.forEach((it, i) => {
    const tr = tbl.insertRow();
    tr.innerHTML = `
      <td><input type="text" value="${esc(it.name)}" oninput="setItem(${i},'name',this.value)"></td>
      <td><input type="text" value="${esc(it.value)}" oninput="setItem(${i},'value',this.value)"></td>
      <td><input type="text" value="${esc(it.unit)}" oninput="setItem(${i},'unit',this.value)"></td>
      <td><input type="text" value="${esc(it.range)}" oninput="setItem(${i},'range',this.value)"></td>
      <td><button class="btn gray" style="padding:2px 6px" onclick="delRow(${i})">×</button></td>`;
  });
}
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function setItem(i, k, v){ state[IMAGES[cur].name].items[i][k] = v; buildTabs(); }
function addRow(){ state[IMAGES[cur].name].items.push({name:'',value:'',unit:'',range:''}); renderItems(); }
function delRow(i){ state[IMAGES[cur].name].items.splice(i,1); renderItems(); }

function download(name, blob) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

function exportAll() {
  const name = IMAGES[cur].name;
  const st = state[name];

  // 1. 脱敏图：原图 + 黑块
  const out = document.createElement('canvas');
  out.width = img.width; out.height = img.height;
  const octx = out.getContext('2d');
  octx.drawImage(img, 0, 0);
  octx.fillStyle = '#000';
  st.masks.forEach(m => octx.fillRect(m.x, m.y, m.w, m.h));
  out.toBlob(b => download(name.replace(/\.png$/i,'') + '_masked.png', b), 'image/png');

  // 2. 标注 JSON
  const valid = st.items.filter(it => it.name.trim());
  const doc = {
    source_image: name,
    masked_image: name.replace(/\.png$/i,'') + '_masked.png',
    report_id: name.replace(/\.png$/i,''),
    masks: st.masks,
    items: valid.map(it => ({
      name: it.name.trim(),
      value: it.value.trim(),
      unit: it.unit.trim(),
      range_raw: it.range.trim()
    }))
  };
  const blob = new Blob([JSON.stringify(doc, null, 2)], {type:'application/json'});
  download(name.replace(/\.png$/i,'') + '_annot.json', blob);

  toast('已导出 ' + valid.length + ' 项');
  setTimeout(() => { if (cur < IMAGES.length - 1) { cur++; loadImage(); } }, 800);
}

loadImage();
window.addEventListener('resize', () => { const t = cv.width; cv.width = t; redraw(); });
</script>
</body>
</html>
'''


def main():
    args = parse_args()
    files = sorted([f for f in glob.glob(os.path.join(args.src, '*'))
                    if os.path.isfile(f) and f.lower().endswith(('.png', '.jpg', '.jpeg'))])
    if not files:
        print(f'{args.src} 下没有图片')
        sys.exit(1)

    print(f'读取 {len(files)} 张图片...')
    images = []
    for f in files:
        with open(f, 'rb') as fp:
            b64 = base64.b64encode(fp.read()).decode('ascii')
        images.append({'name': os.path.basename(f), 'b64': b64})
        print(f'  {os.path.basename(f)}  {len(b64)//1024} KB (base64)')

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, 'output'), exist_ok=True)

    html = HTML_TMPL.replace('__IMAGES__', json.dumps(images, ensure_ascii=False))
    out = os.path.join(args.out_dir, 'annotator.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)

    size_mb = os.path.getsize(out) / 1024 / 1024
    print()
    print('=' * 56)
    print('标注工具已生成')
    print('=' * 56)
    print(f'  {out}  ({size_mb:.1f} MB)')
    print()
    print('使用方法：')
    print('  1. 双击用 Chrome/Edge 打开（文件较大，首次加载稍慢）')
    print('  2. 在图上拖拽框选个人信息区域（姓名/身份证/电话/地址/条形码）')
    print('  3. 右侧逐项填写检验项目')
    print('  4. 点"保存本张"，浏览器下载 脱敏图 + 标注JSON')
    print(f'  5. 把下载的文件移到 {os.path.join(args.out_dir, "output")}')
    print()
    print('注意：这是本地HTML，图片不会上传到任何服务器。')


if __name__ == '__main__':
    main()
