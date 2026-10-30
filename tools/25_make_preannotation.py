# -*- coding: utf-8 -*-
r"""25 本地：生成"预标注"工具（OCR结果已预填，人工只改错）

与 22_make_annotator.py 的区别：
    22 是从零填空表格；25 是把 OCR 识别结果预填进去，人工只需核对修改。
    工作量从"填 60-90 项"降到"改几处错"。

用法：
    # 从 artifacts + 图片生成预标注HTML
    python tools\25_make_preannotation.py ^
        --artifacts <数据目录>\artifacts ^
        --images <图片目录>"""

import os
import sys
import json
import glob
import base64
import argparse
import importlib.util

sys.stdout.reconfigure(encoding='utf-8')


def load_parser():
    """动态导入 04_parse_predictions.py 里的解析函数（文件名带数字，不能直接import）"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, '04_parse_predictions.py')
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location('p04', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--artifacts', default=r'<数据目录>\artifacts')
    ap.add_argument('--images', default=r'<图片目录>')
    ap.add_argument('--out-dir', default=r'<数据目录>\标注工具')
    return ap.parse_args()


def build_preitems(artifacts_dir, images_dir, parser):
    """返回 {图片basename: [items]}，只包含能匹配上的。"""
    # 1. 收集 artifacts 里的 image -> items
    by_image = {}
    for f in sorted(glob.glob(os.path.join(artifacts_dir, '*.json'))):
        if os.path.basename(f).startswith('_'):
            continue
        try:
            rec = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        img = rec.get('image', '')
        if not img:
            continue
        items = []
        if parser:
            try:
                tables = parser.extract_tables(parser.get_html_sources(rec))
                for tb in tables:
                    for row in tb:
                        items.extend(parser.extract_row_items(row))
            except Exception as e:
                print(f'  解析 {img} 失败: {e}')
        by_image[img] = items

    # 2. 与图片目录匹配（按文件名，忽略扩展名差异）
    stem_map = {}
    for img, items in by_image.items():
        stem_map[os.path.splitext(img)[0]] = items

    result = {}
    for f in sorted(glob.glob(os.path.join(images_dir, '*'))):
        if not os.path.isfile(f):
            continue
        if not f.lower().endswith(('.png', '.jpg', '.jpeg')):
            continue
        name = os.path.basename(f)
        stem = os.path.splitext(name)[0]
        result[name] = stem_map.get(stem, [])
    return result


HTML_HEAD = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>预标注复核工具</title>
<style>
* { box-sizing: border-box; }
body { margin:0; font-family:"Microsoft YaHei",sans-serif; background:#1e1e1e; color:#ddd; display:flex; flex-direction:column; height:100vh; }
#tabs { display:flex; background:#2d2d2d; border-bottom:1px solid #444; flex-wrap:wrap; }
#tabs button { padding:8px 14px; background:none; border:none; color:#aaa; cursor:pointer; font-size:13px; border-bottom:2px solid transparent; }
#tabs button.active { color:#4ec9b0; border-bottom-color:#4ec9b0; background:#252526; }
h3 { margin:6px 0 10px; font-size:14px; color:#4ec9b0; }
#items { width:100%; border-collapse:collapse; }
#items th { font-size:11px; color:#888; text-align:left; padding:3px; border-bottom:1px solid #444; }
#items td { padding:2px; }
#items input { width:100%; font-size:12px; padding:3px; background:#3c3c3c; border:1px solid #555; color:#eee; }
#items tr.flagged input { background:#4a3a1a; }
.btn { padding:6px 12px; background:#0e639c; border:none; color:#fff; cursor:pointer; font-size:13px; border-radius:3px; margin:4px 4px 0 0; }
.btn:hover { background:#1177bb; }
.btn.warn { background:#8b2f2f; }
.btn.gray { background:#444; }
#status { position:fixed; bottom:10px; right:10px; background:#0e639c; padding:6px 12px; border-radius:4px; font-size:12px; opacity:0; transition:opacity .3s; }
#status.show { opacity:1; }
.hint { font-size:11px; color:#777; line-height:1.6; margin-top:10px; }
.warnbox { background:#3a2a1a; border-left:3px solid #d19a3a; padding:8px; font-size:12px; margin:8px 0; }
</style>
</head>
<body>
<div id="tabs"></div>
<div id="main">
  <div id="left"><canvas id="cv"></canvas></div>
  <div id="right">
    <div class="warnbox">
      <b>复核要点</b><br>
      · 橙色行 = 该项名称为空或可疑，重点看<br>
      · 参考范围错了就改，图上没有就留空<br>
      · 漏掉的项目点"+ 添加一行"补上<br>
      · 多出来的行点 × 删掉
    </div>

    <h3>脱敏（如原图未脱敏）</h3>
    <label style="font-size:12px;color:#999">在图上拖拽框选姓名/身份证/电话/住址/条形码</label><br>
    <button class="btn gray" onclick="undoMask()">撤销框</button>
    <button class="btn warn" onclick="clearMask()">清空框</button>
    <span class="hint" id="maskinfo" style="margin-left:8px"></span>

    <h3 style="margin-top:16px">识别结果（已预填，请核对）</h3>
    <table id="items">
      <tr><th style="width:36%">项目名称</th><th style="width:20%">结果</th><th style="width:15%">单位</th><th style="width:23%">参考范围</th><th></th></tr>
    </table>
    <button class="btn gray" onclick="addRow()">+ 添加一行</button>

    <h3 style="margin-top:16px">导出</h3>
    <button class="btn" onclick="exportAll()">保存本张并下一张 →</button>

    <div class="hint">
      导出后浏览器会下载脱敏图 + 标注JSON，
      请把文件移到 <数据目录>\标注工具\output\ 目录
    </div>
  </div>
</div>
<div id="status"></div>
<script>
const IMAGES = __IMAGES__;
const PREITEMS = __PREITEMS__;

let cur = 0;
const state = {};
IMAGES.forEach(im => {
  const pre = (PREITEMS[im.name] || []).map(it => ({
    name: it.item_name || '', value: it.item_value || '',
    unit: it.item_unit || '', range: it.item_range || ''
  }));
  if (!pre.length) pre.push({name:'',value:'',unit:'',range:''});
  state[im.name] = {masks:[], items: pre};
});

const tabs = document.getElementById('tabs');
const cv = document.getElementById('cv');
const ctx = cv.getContext('2d');
let img = new Image();
let scale = 1, dragging=false, startX=0, startY=0, curRect=null;

function toast(m){ const s=document.getElementById('status'); s.textContent=m; s.classList.add('show'); setTimeout(()=>s.classList.remove('show'),1600); }
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

function buildTabs(){
  tabs.innerHTML='';
  IMAGES.forEach((im,i)=>{
    const b=document.createElement('button');
    b.textContent = im.name.replace(/^TempDragFile_/,'').replace(/\.(png|jpg|jpeg)$/i,'');
    if(i===cur) b.classList.add('active');
    if(state[im.name]._done) b.classList.add('done');
    b.onclick=()=>{ cur=i; loadImage(); };
    tabs.appendChild(b);
  });
}

function loadImage(){
  buildTabs();
  img=new Image();
  img.onload=()=>{
    cv.width=img.width; cv.height=img.height;
    const box=document.getElementById('left');
    const s=Math.min(1,(box.clientWidth-24)/img.width,(box.clientHeight-24)/img.height);
    cv.style.width=(img.width*s)+'px'; cv.style.height=(img.height*s)+'px';
    scale=s; redraw(); renderItems(); updateMaskInfo();
  };
  img.src='data:image/png;base64,'+IMAGES[cur].b64;
}

function redraw(){
  ctx.clearRect(0,0,cv.width,cv.height);
  ctx.drawImage(img,0,0);
  ctx.fillStyle='#000';
  state[IMAGES[cur].name].masks.forEach(m=>ctx.fillRect(m.x,m.y,m.w,m.h));
  if(curRect){ ctx.strokeStyle='#ff5555'; ctx.lineWidth=3; ctx.strokeRect(curRect.x,curRect.y,curRect.w,curRect.h); }
}

function evPos(e){ const r=cv.getBoundingClientRect(); return {x:(e.clientX-r.left)/scale, y:(e.clientY-r.top)/scale}; }
cv.addEventListener('mousedown',e=>{ const p=evPos(e); dragging=true; startX=p.x; startY=p.y; curRect={x:p.x,y:p.y,w:0,h:0}; });
cv.addEventListener('mousemove',e=>{ if(!dragging)return; const p=evPos(e); curRect={x:Math.min(startX,p.x),y:Math.min(startY,p.y),w:Math.abs(p.x-startX),h:Math.abs(p.y-startY)}; redraw(); });
cv.addEventListener('mouseup',e=>{ if(!dragging)return; dragging=false; if(curRect&&curRect.w>4&&curRect.h>4) state[IMAGES[cur].name].masks.push(curRect); curRect=null; redraw(); updateMaskInfo(); });
function undoMask(){ const m=state[IMAGES[cur].name].masks; if(m.length){m.pop(); redraw(); updateMaskInfo();} }
function clearMask(){ state[IMAGES[cur].name].masks=[]; redraw(); updateMaskInfo(); }
function updateMaskInfo(){ document.getElementById('maskinfo').textContent='已框选 '+state[IMAGES[cur].name].masks.length+' 处'; }

function renderItems(){
  const tbl=document.getElementById('items');
  while(tbl.rows.length>1) tbl.deleteRow(1);
  const items=state[IMAGES[cur].name].items;
  items.forEach((it,i)=>{
    const tr=tbl.insertRow();
    const suspect = !it.name.trim();
    if(suspect) tr.className='flagged';
    tr.innerHTML=`
      <td><input value="${esc(it.name)}" oninput="setItem(${i},'name',this.value)"></td>
      <td><input value="${esc(it.value)}" oninput="setItem(${i},'value',this.value)"></td>
      <td><input value="${esc(it.unit)}" oninput="setItem(${i},'unit',this.value)"></td>
      <td><input value="${esc(it.range)}" oninput="setItem(${i},'range',this.value)"></td>
      <td><button class="btn gray" style="padding:2px 6px" onclick="delRow(${i})">×</button></td>`;
  });
}
function setItem(i,k,v){ state[IMAGES[cur].name].items[i][k]=v; }
function addRow(){ state[IMAGES[cur].name].items.push({name:'',value:'',unit:'',range:''}); renderItems(); }
function delRow(i){ state[IMAGES[cur].name].items.splice(i,1); renderItems(); }

function download(name,blob){
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name;
  document.body.appendChild(a); a.click(); setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},1000);
}

function exportAll(){
  const name=IMAGES[cur].name, st=state[name];
  const out=document.createElement('canvas');
  out.width=img.width; out.height=img.height;
  const o=out.getContext('2d');
  o.drawImage(img,0,0); o.fillStyle='#000';
  st.masks.forEach(m=>o.fillRect(m.x,m.y,m.w,m.h));
  out.toBlob(b=>download(name.replace(/\.(png|jpg|jpeg)$/i,'')+'_masked.png',b),'image/png');

  const valid=st.items.filter(x=>x.name.trim());
  const doc={
    source_image:name,
    masked_image:name.replace(/\.(png|jpg|jpeg)$/i,'')+'_masked.png',
    report_id:name.replace(/\.(png|jpg|jpeg)$/i,''),
    masks:st.masks,
    items:valid.map(x=>({name:x.name.trim(),value:x.value.trim(),unit:x.unit.trim(),range_raw:x.range.trim()}))
  };
  download(name.replace(/\.(png|jpg|jpeg)$/i,'')+'_annot.json',
           new Blob([JSON.stringify(doc,null,2)],{type:'application/json'}));
  st._done=true; buildTabs();
  toast('已导出 '+valid.length+' 项');
  setTimeout(()=>{ if(cur<IMAGES.length-1){cur++; loadImage();} },800);
}

loadImage();
</script>
</body>
</html>
'''


def main():
    args = parse_args()
    imgs = sorted([f for f in glob.glob(os.path.join(args.images, '*'))
                   if os.path.isfile(f) and f.lower().endswith(('.png', '.jpg', '.jpeg'))])
    if not imgs:
        print(f'{args.images} 下没有图片')
        sys.exit(1)

    parser = load_parser()
    if parser is None:
        print('!! 找不到 04_parse_predictions.py，将生成空白表格')
    preitems = build_preitems(args.artifacts, args.images, parser)

    hit = sum(1 for v in preitems.values() if v)
    print(f'图片 {len(imgs)} 张，其中 {hit} 张有预填结果')
    for name, items in preitems.items():
        print(f'  {name}: {len(items)} 项' + ('' if items else '  （无OCR结果，空白表格）'))
    if hit == 0:
        print()
        print('!! 没有任何预填结果。需要先对这批图跑 OCR：')
        print('   AI Studio: python tools/03_export_ocr.py --gold-dir gold --src . --out-dir artifacts')
        print('   或本地（有GPU时）: $env:PADDLE_DEVICE="gpu"; python tools\\03_export_ocr.py ...')
        print('   然后把 artifacts 下载回来，重跑本脚本')

    images = []
    for f in imgs:
        with open(f, 'rb') as fp:
            images.append({'name': os.path.basename(f),
                           'b64': base64.b64encode(fp.read()).decode('ascii')})

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, 'output'), exist_ok=True)

    html = HTML_HEAD.replace('__IMAGES__', json.dumps(images, ensure_ascii=False))
    html = html.replace('__PREITEMS__', json.dumps(preitems, ensure_ascii=False))
    out = os.path.join(args.out_dir, 'preannotate.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)

    print()
    print('=' * 58)
    print('预标注工具已生成')
    print('=' * 58)
    print(f'  {out}  ({os.path.getsize(out)/1024/1024:.1f} MB)')
    print()
    print('用法：')
    print('  1. 浏览器打开，逐张核对识别结果（橙色行是可疑项）')
    print('  2. 改错、补漏、删多余')
    print('  3. 点"保存本张并下一张"，下载 脱敏图 + 标注JSON')
    print('  4. 把下载的文件移到 标注工具\\output\\')
    print('  5. 跑 python tools\\23_gold_from_annotation.py 生成 gold')


if __name__ == '__main__':
    main()
