# -*- coding: utf-8 -*-
"""主界面页面（单页 HTML + 内联 JS/CSS）。

核心交互就一句话：**拖入任意文件 → 自动列出「能转成什么」→ 选一个 → 转换。**

设计上的两个取舍：
* 按扩展名**分组**渲染。拖进来一堆混合文件时，PNG 那组只显示 PNG 能转的格式，
  PDF 那组只显示 PDF 的 —— 不把用户不需要的按钮塞给他。
* 参数（质量、DPI、页码范围…）藏在「⚙」后面。90% 的情况下用户只想点一下就走，
  默认值就应该能出好结果；需要精细控制的人再展开。
"""
from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>格式转换工作台</title>
<style>
*{box-sizing:border-box}
:root{
  --bg:#f6f7f9; --card:#fff; --fg:#1b1d21; --dim:#6b7280; --line:#e3e5e9;
  --accent:#2563eb; --accent-fg:#fff; --ok:#15803d; --err:#b91c1c;
  --soft:#eef1f5;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#16181c;--card:#1e2126;--fg:#e8eaee;--dim:#9aa1ac;--line:#2e3238;
        --accent:#3b82f6;--ok:#4ade80;--err:#f87171;--soft:#262a30}
}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.6 "Microsoft YaHei",-apple-system,Segoe UI,sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:22px 20px 60px}
h1{font-size:17px;font-weight:600;margin:0 0 4px}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px;margin-bottom:14px}
.card h2{font-size:13px;font-weight:600;margin:0 0 10px;color:var(--dim);
         letter-spacing:.3px}

/* 引擎状态条 */
.engines{display:flex;gap:8px;flex-wrap:wrap;font-size:12px}
.eng{display:flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;
     background:var(--soft);color:var(--dim)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:0 0 auto}
.dot.off{background:var(--err)}

/* 能力总览（功能导航） */
#caps .caps-head{display:flex;align-items:center;justify-content:space-between;cursor:pointer;
                user-select:none}
#caps .caps-head .t{font-size:13px;font-weight:600}
#caps .caps-head .more{color:var(--accent);font-size:12px}
#caps .caps-body{margin-top:12px}
.caprow{display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--line);align-items:flex-start}
.caprow:first-child{border-top:0}
.caprow .cat{flex:0 0 128px;font-size:12.5px;font-weight:600;line-height:1.5}
.caprow .cat .en{display:block;font-weight:400;color:var(--dim);font-size:11px}
.caprow .desc{flex:1;font-size:12px;color:var(--dim);line-height:1.6}
.caprow .st{flex:0 0 auto;font-size:11px;padding:2px 8px;border-radius:20px;white-space:nowrap}
.st.ok{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.st.off{background:color-mix(in srgb,var(--err) 14%,transparent);color:var(--err)}
#caps.folded .caps-body{display:none}
#caps.folded .more::after{content:" ▾"}
#caps:not(.folded) .more::after{content:" ▴"}

/* 拖放区 */
#drop{border:2px dashed var(--line);border-radius:12px;padding:34px 20px;text-align:center;
      color:var(--dim);transition:.15s;cursor:pointer}
#drop.hot{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,transparent);
          color:var(--accent)}
#drop b{display:block;font-size:15px;color:var(--fg);font-weight:600;margin-bottom:6px}
#drop.hot b{color:var(--accent)}

/* 队列 */
.qhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.qhead .n{font-size:13px;font-weight:600}
.group{background:var(--card);border:1px solid var(--line);border-radius:8px;
       padding:12px;margin-bottom:9px}
.group:last-child{margin-bottom:0}
.ghead{display:flex;align-items:center;gap:8px;margin-bottom:9px;font-size:12.5px}
.ext{background:var(--accent);color:var(--accent-fg);border-radius:5px;
     padding:1px 7px;font-weight:600;font-size:11.5px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:11px}
.chip{display:flex;align-items:center;gap:6px;background:var(--soft);
      border-radius:6px;padding:3px 6px 3px 9px;font-size:12px;max-width:100%}
.chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}
.chip i{color:var(--dim);font-style:normal;font-size:11px}
.chip button{border:0;background:none;color:var(--dim);cursor:pointer;
             font-size:14px;line-height:1;padding:0 2px;border-radius:3px}
.chip button:hover{color:var(--err);background:var(--soft)}
.tgts{display:flex;flex-wrap:wrap;gap:7px}
.tgts button{border:1px solid var(--line);background:var(--card);color:var(--fg);
             border-radius:7px;padding:6px 12px;font-size:12.5px;cursor:pointer;
             font-family:inherit;transition:.12s}
.tgts button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.tgts button:disabled{opacity:.45;cursor:not-allowed}
.tgts button.one2many::after{content:" ⌗";color:var(--dim)}
.tgts button.multi::after{content:" ⊕";color:var(--dim)}

/* 输出行 */
.outrow{display:flex;gap:8px;align-items:center}
.outrow label{font-size:12.5px;color:var(--dim);white-space:nowrap}
.outrow input{flex:1;min-width:0;border:1px solid var(--line);background:#2f343b;
              color:var(--fg);border-radius:7px;padding:7px 10px;font:inherit;font-size:12.5px}
.outrow input::placeholder{color:var(--dim)}
.outrow button{border:1px solid var(--line);background:var(--card);color:var(--fg);
               border-radius:7px;padding:7px 12px;font:inherit;font-size:12.5px;
               cursor:pointer;white-space:nowrap}
.outrow button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.outrow button:disabled{opacity:.55;cursor:not-allowed}
.hint{font-size:12px;color:var(--dim);margin-top:8px}
.hint.warn{color:var(--err)}
.hint a{color:var(--accent);cursor:pointer;text-decoration:underline;margin-left:6px}

/* 结果 */
.res{display:flex;gap:9px;padding:7px 0;border-bottom:1px solid var(--line);
     font-size:12.5px;align-items:baseline}
.res:last-child{border-bottom:0}
.res .m{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.res .s{color:var(--dim);font-size:11.5px;white-space:nowrap}
.res.ok .m::before{content:"✓ ";color:var(--ok);font-weight:600}
.res.err .m::before{content:"✕ ";color:var(--err);font-weight:600}
.res.err .m{color:var(--err)}

/* 选项弹层 */
/* 用 fixed 而不是 absolute：absolute+inset:0 的参照是文档初始视口，页面滚下去后
   弹层会停在顶部看不见。fixed 才跟随当前视口，永远居中在用户眼前。 */
.mask{position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;
      align-items:center;justify-content:center;z-index:9;padding:20px}
.dlg{background:var(--card);border-radius:11px;padding:18px;width:min(430px,100%);
     border:1px solid var(--line);max-height:88vh;overflow:auto}
.dlg h3{margin:0 0 3px;font-size:14px}
.dlg .d{color:var(--dim);font-size:12px;margin-bottom:14px}
.fld{margin-bottom:12px}
.fld label{display:block;font-size:12.5px;margin-bottom:4px}
.fld .h{color:var(--dim);font-size:11.5px;margin-top:3px}
.fld input[type=text],.fld input[type=number],.fld select{
  width:100%;border:1px solid var(--line);background:var(--bg);color:var(--fg);
  border-radius:7px;padding:7px 9px;font:inherit;font-size:12.5px}
.fld .cb{display:flex;align-items:center;gap:7px}
.dlg .acts{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.dlg .acts button{border:1px solid var(--line);background:var(--card);color:var(--fg);
                  border-radius:7px;padding:7px 15px;font:inherit;font-size:12.5px;cursor:pointer}
.dlg .acts .p{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.dlg .acts button:hover{opacity:.88}
[hidden]{display:none !important}
.busy{display:inline-block;width:11px;height:11px;border:2px solid var(--line);
     border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;
     vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
.sig{text-align:center;color:var(--dim);font-size:11.5px;margin-top:18px;
     opacity:.75;letter-spacing:.2px}
</style>
</head>
<body>
<div class="wrap">
  <h1>格式转换工作台</h1>
  <div class="sub">拖进文件，选个目标格式就行</div>

  <div class="card">
    <h2>引擎</h2>
    <div class="engines" id="engines"></div>
  </div>

  <div class="card" id="caps">
    <div class="caps-head" id="capshead">
      <span class="t">支持的功能</span>
      <span class="more">展开看全部</span>
    </div>
    <div class="caps-body" id="capsbody"></div>
  </div>

  <div class="card">
    <div id="drop">
      <b>把文件拖到这里</b>
      支持图片、PDF、音视频、Office 文档、NCM；可一次拖多个
      <div style="margin-top:12px"><button id="pickbtn" class="p" style="border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:7px;padding:7px 14px;font:inherit;font-size:12.5px;cursor:pointer">或者点这里选文件</button></div>
      <input type="file" id="picker" multiple hidden>
    </div>
  </div>

  <div class="card" id="qcard" hidden>
    <div class="qhead">
      <span class="n" id="qstat"></span>
      <button id="clr" style="border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:7px;padding:5px 11px;font:inherit;font-size:12px;cursor:pointer">清空</button>
    </div>
    <div id="groups"></div>
  </div>

  <div class="card">
    <div class="outrow">
      <label>输出到</label>
      <input type="text" id="outdir" value="__OUTDIR__">
      <button id="browse">选择文件夹…</button>
    </div>
    <div class="hint" id="outdirhint" hidden></div>
  </div>

  <div class="card" id="rcard" hidden>
    <h2>转换结果 <span id="rstat" style="font-weight:400"></span></h2>
    <div id="results"></div>
  </div>

  <div class="sig">Na1aB 制作的小工具 v1.4</div>
</div>

<script>
var TOKEN = "__TOKEN__";
var $ = function(id){ return document.getElementById(id); };
function api(p){ return "/" + TOKEN + p; }

/* ---------- 状态 ---------- */
var items = [];      // {id, name, size, ext, targets}
var busy = false;

function esc(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function fmtSize(n){
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n/1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n/1048576).toFixed(1) + " MB";
  return (n/1073741824).toFixed(2) + " GB";
}

/* ---------- 引擎状态 ---------- */
function loadState(){
  fetch(api("/api/state")).then(function(r){ return r.json(); }).then(function(j){
    var html = "";
    (j.engines || []).forEach(function(e){
      html += '<span class="eng" title="' + esc(e.hint || "") + '">'
            + '<i class="dot' + (e.found ? "" : " off") + '"></i>'
            + esc(e.label) + (e.found ? "" : " · 未安装") + '</span>';
    });
    (j.plugins || []).forEach(function(p){
      if (!p.ok) html += '<span class="eng" title="' + esc(p.reason || "") + '">'
                       + '<i class="dot off"></i>' + esc(p.label) + '</span>';
    });
    $("engines").innerHTML = html || '<span class="eng">没有可用插件</span>';
  }).catch(function(){});
}
loadState();

/* ---------- 能力总览（功能导航） ---------- */
function loadCaps(){
  fetch(api("/api/capabilities")).then(function(r){ return r.json(); }).then(function(j){
    var groups = j.groups || [];
    var html = "";
    groups.forEach(function(g){
      html += '<div class="caprow">'
            + '<div class="cat">' + esc(g.label)
            +   (g.engine ? '<span class="en">' + esc(g.engine) + '</span>' : '') + '</div>'
            + '<div class="desc">' + esc(g.note) + '</div>'
            + '<span class="st ' + (g.ok ? "ok" : "off") + '">'
            +   (g.ok ? "可用" : (g.reason ? "缺引擎" : "不可用")) + '</span>'
            + '</div>';
    });
    $("capsbody").innerHTML = html || '<div class="desc">暂无可用的转换功能</div>';
  }).catch(function(){});
}
loadCaps();

$("capshead").onclick = function(){
  $("caps").classList.toggle("folded");
  $("capshead").querySelector(".more").textContent =
    $("caps").classList.contains("folded") ? "展开看全部" : "收起";
};
// 默认收起，只露一行，别把拖放区挤下去
$("caps").classList.add("folded");

/* ---------- 拖入 ---------- */
var drop = $("drop");
["dragenter","dragover"].forEach(function(ev){
  drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add("hot"); });
});
["dragleave","drop"].forEach(function(ev){
  drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.remove("hot"); });
});
drop.addEventListener("drop", function(e){
  if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
});
drop.addEventListener("click", function(e){
  if (e.target.id !== "pickbtn") $("picker").click();
});
$("pickbtn").onclick = function(e){ e.stopPropagation(); $("picker").click(); };
$("picker").onchange = function(){ addFiles(this.files); this.value = ""; };

function addFiles(list){
  var arr = Array.prototype.slice.call(list || []);
  if (!arr.length) return;
  setOutHint("正在读取 " + arr.length + " 个文件…");
  arr.forEach(function(f){ uploadOne(f); });
}

function uploadOne(f){
  var url = api("/api/add") + "?name=" + encodeURIComponent(f.name);
  fetch(url, { method: "POST", body: f })
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (!j.ok) { setOutHint("「" + f.name + "」读取失败：" + (j.msg || ""), true); return; }
      items.push(j);
      renderQueue();
      setOutHint("");
    })
    .catch(function(e){ setOutHint("「" + f.name + "」上传失败：" + e, true); });
}

/* ---------- 队列渲染（按扩展名分组） ---------- */
function groupsOf(){
  var g = {}, order = [];
  items.forEach(function(it){
    if (!g[it.ext]) { g[it.ext] = []; order.push(it.ext); }
    g[it.ext].push(it);
  });
  return { map: g, order: order };
}

function renderQueue(){
  var G = groupsOf();
  $("qcard").hidden = items.length === 0;
  if (!items.length) { $("groups").innerHTML = ""; $("qstat").textContent = ""; return; }

  $("qstat").textContent = "共 " + items.length + " 个文件 · "
                         + G.order.length + " 种类型";

  var html = "";
  G.order.forEach(function(ext){
    var list = G.map[ext];
    var t = list[0].targets || [];

    html += '<div class="group">';
    html += '<div class="ghead"><span class="ext">' + esc(ext.toUpperCase()) + '</span>'
          + '<span style="color:var(--dim)">' + list.length + ' 个文件 · '
          + t.length + ' 种可转格式</span></div>';

    html += '<div class="chips">';
    list.forEach(function(it){
      html += '<span class="chip" title="' + esc(it.name) + '">'
            + '<span>' + esc(it.name) + '</span><i>' + fmtSize(it.size) + '</i>'
            + '<button data-rm="' + esc(it.id) + '">×</button></span>';
    });
    html += '</div>';

    if (!t.length) {
      html += '<div class="hint">这种格式暂时没有可用的转换目标</div>';
    } else {
      html += '<div class="tgts">';
      t.forEach(function(x){
        var cls = (x.one2many ? " one2many" : "") + (x.multi ? " multi" : "");
        html += '<button class="' + cls.trim() + '" data-ext="' + esc(ext) + '"'
              + ' data-dst="' + esc(x.dst) + '" data-action="' + esc(x.action) + '"'
              + (x.ok ? "" : ' disabled title="' + esc(x.reason) + '"')
              + '>' + esc(x.label) + '</button>';
      });
      html += '</div>';
    }
    html += '</div>';
  });
  $("groups").innerHTML = html;

  Array.prototype.forEach.call($("groups").querySelectorAll("[data-rm]"), function(b){
    b.onclick = function(){ removeItem(b.getAttribute("data-rm")); };
  });
  Array.prototype.forEach.call($("groups").querySelectorAll(".tgts button"), function(b){
    b.onclick = function(){ startConvert(b); };
  });
}

function removeItem(id){
  var gone = null;
  for (var i = 0; i < items.length; i++){
    if (items[i].id === id) { gone = items.splice(i, 1)[0]; break; }
  }
  if (!gone) return;
  fetch(api("/api/drop"), { method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ id: gone.id }) }).catch(function(){});
  renderQueue();
}

/* 清空：整个数组一次换掉，不逐个 splice。
   （在 forEach 里删元素会跳项 —— v1.0 踩过，4 个只清掉 2 个。） */
$("clr").onclick = function(){
  var ids = items.filter(function(x){ return x.id; }).map(function(x){ return x.id; });
  items = [];
  renderQueue();
  $("rcard").hidden = true;
  if (ids.length){
    fetch(api("/api/drop"), { method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ ids: ids }) }).catch(function(){});
  }
};

/* ---------- 选项弹层 ---------- */
function findTarget(ext, dst, action){
  var list = null;
  for (var i = 0; i < items.length; i++){
    if (items[i].ext === ext) { list = items[i].targets; break; }
  }
  if (!list) return null;
  for (var k = 0; k < list.length; k++){
    if (list[k].dst === dst && list[k].action === action) return list[k];
  }
  return null;
}

/* 关闭选项弹层：清掉页面上所有 .mask（防重复 id 残留，只删一个会留另一个）。 */
function closeMask(){
  var ms = document.querySelectorAll(".mask");
  for (var i = 0; i < ms.length; i++) ms[i].remove();
}

function startConvert(btn){
  if (busy) return;
  var ext = btn.getAttribute("data-ext");
  var dst = btn.getAttribute("data-dst");
  var action = btn.getAttribute("data-action");
  var t = findTarget(ext, dst, action);
  if (!t) return;

  if (!t.options || !t.options.length) { doConvert(ext, dst, action, {}, t.label); return; }

  // 先关掉可能残留的旧弹层，再开新的
  closeMask();
  // 有参数：先弹个面板。默认值就能出好结果，所以只是"想调才调"。
  var html = '<div class="mask" id="mask"><div class="dlg">'
           + '<h3>' + esc(t.label) + '</h3>'
           + '<div class="d">留空用默认值即可</div>';
  t.options.forEach(function(o){
    html += '<div class="fld">';
    if (o.kind === "bool"){
      html += '<label class="cb"><input type="checkbox" data-k="' + esc(o.key) + '"'
            + (o.default ? " checked" : "") + '> ' + esc(o.label) + '</label>';
    } else if (o.kind === "choice"){
      html += '<label>' + esc(o.label) + '</label>'
            + '<select data-k="' + esc(o.key) + '">';
      (o.choices || []).forEach(function(c){
        html += '<option value="' + esc(c.value) + '"'
              + (String(c.value) === String(o.default) ? " selected" : "") + '>'
              + esc(c.text) + '</option>';
      });
      html += '</select>';
    } else {
      html += '<label>' + esc(o.label) + '</label>'
            + '<input type="' + (o.kind === "int" ? "number" : "text") + '"'
            + ' data-k="' + esc(o.key) + '" value="' + esc(o.default === null ? "" : o.default) + '"'
            + (o.kind === "int" && o.lo !== null ? ' min="' + o.lo + '"' : '')
            + (o.kind === "int" && o.hi !== null ? ' max="' + o.hi + '"' : '') + '>';
    }
    if (o.hint) html += '<div class="h">' + esc(o.hint) + '</div>';
    html += '</div>';
  });
  html += '<div class="acts"><button id="cx">取消</button>'
        + '<button id="cok" class="p">开始转换</button></div></div></div>';
  $("qcard").insertAdjacentHTML("afterend", html);

  $("cx").onclick = function(){ closeMask(); };
  $("mask").onclick = function(e){ if (e.target.id === "mask") closeMask(); };
  $("cok").onclick = function(){
    var opts = {};
    Array.prototype.forEach.call($("mask").querySelectorAll("[data-k]"), function(el){
      var k = el.getAttribute("data-k");
      opts[k] = (el.type === "checkbox") ? el.checked : el.value;
    });
    closeMask();
    doConvert(ext, dst, action, opts, t.label);
  };
}

/* ---------- 执行转换 ---------- */
function doConvert(ext, dst, action, opts, label){
  var ids = items.filter(function(x){ return x.ext === ext; }).map(function(x){ return x.id; });
  if (!ids.length) return;

  busy = true;
  $("rcard").hidden = false;
  $("rstat").textContent = "";
  $("results").innerHTML = '<div class="res"><div class="m"><i class="busy"></i>'
                         + esc(label) + ' 处理中…</div></div>';

  fetch(api("/api/convert"), {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ ids: ids, dst: dst, action: action, opts: opts,
                           out: $("outdir").value.trim() })
  })
    .then(function(r){ return r.json(); })
    .then(function(j){
      var list = j.results || [];
      var good = 0, html = "";
      list.forEach(function(x){
        if (x.ok) good++;
        html += '<div class="res ' + (x.ok ? "ok" : "err") + '">'
              + '<div class="m" title="' + esc(x.name || x.msg) + '">'
              + esc(x.ok ? (x.src ? x.src + " → " : "") + x.name : (x.src ? x.src + "：" : "") + x.msg)
              + '</div>'
              + '<div class="s">' + (x.ok ? fmtSize(x.size) : "") + '</div></div>';
      });
      $("results").innerHTML = html || '<div class="res err"><div class="m">没有任何产出</div></div>';
      $("rstat").textContent = "（" + good + " / " + list.length + " 成功）";
      if (j.out) setOutHint("已保存到 " + j.out);
    })
    .catch(function(e){
      $("results").innerHTML = '<div class="res err"><div class="m">请求失败：' + esc(e) + '</div></div>';
    })
    .then(function(){
      busy = false;
      loadState();
    });
}

/* ---------- 输出目录 ---------- */
function setOutHint(msg, warn){
  var el = $("outdirhint");
  if (!msg) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.className = "hint" + (warn ? " warn" : "");
  el.innerHTML = esc(msg);
}

var picking = false;
$("browse").onclick = function(){
  if (picking) return;
  picking = true;
  var b = $("browse");
  b.disabled = true; b.textContent = "选择中…";
  setOutHint("正在等待你选文件夹…（窗口已弹出的话，它一定在最前面）"
             + '<a id="pk">没看到窗口？点这里强制关闭</a>');
  $("pk").onclick = function(){
    setOutHint("正在关闭…");
    fetch(api("/api/pick-cancel"), { method: "POST" }).catch(function(){});
  };
  var slow = setTimeout(function(){
    if (picking){
      setOutHint("还没选好吗？如果屏幕上确实没有「浏览文件夹」窗口，"
                 + '点这里强制关闭<a id="pk2">点我</a>，或直接在输入框里粘贴路径。', true);
      $("pk2").onclick = function(){
        setOutHint("正在关闭…");
        fetch(api("/api/pick-cancel"), { method: "POST" }).catch(function(){});
      };
    }
  }, 15000);

  fetch(api("/api/pick"), {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ path: $("outdir").value.trim() })
  })
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (j.ok) { $("outdir").value = j.path; setOutHint(""); }
      else { setOutHint((j.msg || "没有选择目录") + "（也可以直接在输入框里粘路径）"); }
    })
    .catch(function(e){ setOutHint("调用系统对话框失败：" + e, true); })
    .then(function(){
      clearTimeout(slow);
      picking = false;
      b.disabled = false; b.textContent = "选择文件夹…";
    });
};

/* ---------- 关闭窗口 = 退出（关浏览器不算，刷新也不算） ---------- */
var leaving = false;
window.addEventListener("pagehide", function(){
  if (leaving) return;
  leaving = true;
  try {
    var blob = new Blob([JSON.stringify({ bye: 1 })], {type: "application/json"});
    navigator.sendBeacon(api("/api/bye"), blob);
  } catch (e) {
    try { fetch(api("/api/bye"), {method:"POST", keepalive:true}); } catch (e2) {}
  }
});
</script>
</body>
</html>
"""
