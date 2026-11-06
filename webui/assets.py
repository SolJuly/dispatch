# -*- coding: utf-8 -*-
"""控制台静态资源：CSS 与 JS 各一份，由 /static/* 直接返回（无构建链）。"""

CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1b1f24;--mut:#606a76;--line:#e3e6ea;
--card:#fff;--pri:#0b62d6;--ok:#0f7b3f;--warn:#9a6b00;--bad:#b3261e;--idle:#5b6470}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--pri);text-decoration:none}a:hover{text-decoration:underline}
header.top{position:sticky;top:0;z-index:10;background:var(--card);border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:18px;padding:10px 18px}
header.top .brand{font-weight:700}
nav.app{display:flex;gap:14px;flex-wrap:wrap}
nav.app a{padding:6px 10px;border-radius:8px;color:var(--mut)}
nav.app a.on{background:#eaf1fc;color:var(--pri);font-weight:600}
nav.app a.off{opacity:.45;pointer-events:none}
header.top .right{margin-left:auto;display:flex;gap:10px;align-items:center;font-size:13px;color:var(--mut)}
main{max-width:1200px;margin:0 auto;padding:22px 18px 72px}
h1{font-size:22px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:390px 1fr;gap:18px;align-items:start}
@media(max-width:960px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:15px;margin:0 0 12px;color:var(--mut);font-weight:600}
.field{margin-bottom:12px}
.field label{display:block;font-size:13px;color:var(--mut);margin-bottom:4px}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--fg);font:inherit}
textarea{resize:vertical}
.hint{font-size:12px;color:var(--mut);margin-top:3px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid transparent}
.pill.ok{background:#e7f5ec;color:var(--ok);border-color:#bfe3cb}
.pill.warn{background:#fdf3d8;color:var(--warn);border-color:#f0dfae}
.pill.bad{background:#fdeae8;color:var(--bad);border-color:#f3c6c1}
.pill.run{background:#e8f0fd;color:var(--pri);border-color:#c8dbfa}
.pill.idle{background:#eef1f4;color:var(--idle);border-color:#dde2e7}
.btn{display:inline-block;padding:8px 14px;border:1px solid var(--line);border-radius:9px;
background:var(--card);color:var(--fg);cursor:pointer;font:inherit}
.btn.pri{background:var(--pri);border-color:var(--pri);color:#fff}
.btn:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.results{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.thumb{background:#11151a;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
.thumb img{display:block;width:100%;height:240px;object-fit:contain}
.thumb .meta{background:var(--card);padding:8px 10px;font-size:12px;display:flex;gap:8px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:9px 11px;border-bottom:1px solid #eef1f4;font-size:13.5px;vertical-align:middle}
th{background:#fafbfc;color:var(--mut);font-weight:600}
tr:last-child td{border-bottom:none}
table.kv th{width:180px}
.state{font-size:14px;margin:6px 0}
.timeline{list-style:none;padding:0;margin:0;font-size:13px;color:var(--mut)}
.timeline li{padding:4px 0;border-bottom:1px dashed #eef1f4}
.empty{color:var(--mut);font-size:14px;padding:18px;text-align:center}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12.5px}
.toast{position:fixed;right:18px;bottom:18px;background:#1b1f24;color:#fff;padding:10px 14px;
border-radius:10px;font-size:13px;opacity:0;transform:translateY(8px);transition:.2s}
.toast.on{opacity:1;transform:none}
.muted{color:var(--mut)}
.recs{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.rec{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.rec img{display:block;width:100%;height:110px;object-fit:cover;background:#11151a}
.rec .ph{display:flex;align-items:center;justify-content:center;height:110px;color:var(--mut);font-size:12px}
.rec .recm{display:flex;gap:6px;align-items:center;padding:6px 8px;font-size:12px;flex-wrap:wrap}
.cmp{position:relative;border-radius:12px;overflow:hidden;background:#11151a;margin-top:12px}
.cmp img{display:block;width:100%;height:auto}
.cmp .after{position:absolute;inset:0;clip-path:inset(0 0 0 50%)}
.cmp input[type=range]{position:absolute;left:0;right:0;bottom:8px;width:calc(100% - 16px);margin:0 8px}
.cmp .lbl{position:absolute;top:8px;font-size:12px;background:rgba(0,0,0,.55);color:#fff;padding:2px 8px;border-radius:6px}
.cmp .lbl.a{left:8px}.cmp .lbl.b{right:8px}
.maskwrap{display:inline-block;background:#000;border:1px solid var(--line);border-radius:10px;overflow:hidden}
canvas.maskcanvas{display:block;width:100%;max-width:360px;height:auto;cursor:crosshair;touch-action:none}

/* ---- 2026-11 视觉整理：页头 / 页签 / 分组表单 ---- */
.page-head{margin:2px 0 14px}
.page-head h1{margin:0 0 4px}
.page-head .sub{margin:0;color:var(--mut);font-size:13.5px}
.tabs{display:flex;gap:6px;margin:0 0 16px;flex-wrap:wrap}
.tab{padding:7px 14px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--mut);font-size:14px}
.tab.on{background:var(--pri);border-color:var(--pri);color:#fff;font-weight:600}
.tab:hover{text-decoration:none;border-color:var(--pri)}
.wf-meta{display:flex;gap:8px;align-items:center;margin:2px 0 8px;font-size:12.5px;color:var(--mut);flex-wrap:wrap}
.wf-meta code{background:#eef1f4;padding:1px 6px;border-radius:5px}
.desc{margin:0 0 14px;color:var(--mut);font-size:13px;line-height:1.55}
.grp{border:1px solid var(--line);border-radius:10px;padding:12px 12px 4px;margin:0 0 12px}
.grp legend{padding:0 6px;font-size:12.5px;color:var(--mut);font-weight:600}
.grp .field:last-child{margin-bottom:8px}
.actions{border-top:1px dashed var(--line);padding-top:12px;margin-top:4px}
.check{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--mut);margin-bottom:10px}
.check input{width:auto}
.notice{background:#fff8e6;border:1px solid #f1dfae;color:#7a5b00;border-radius:10px;padding:10px 12px;font-size:13px;margin:0 0 14px}
.sec{font-size:15px;color:var(--mut);margin:26px 0 10px;font-weight:600}
.key{font-size:11px;color:var(--mut);background:#f1f3f6;padding:0 5px;border-radius:4px;font-weight:400}
/* 表单卡片更紧凑，结果卡片粘住 */
.form h2{margin-top:0}
.result{position:sticky;top:74px}
@media(max-width:960px){.result{position:static}}
"""

JS = r"""
(function(){
  const BASE = window.__BASE__ || "";
  const wf = window.__WF__ || null;
  const $ = (s, r) => (r||document).querySelector(s);
  const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  let timer = null, deadline = 0;

  function toast(msg){
    let t = $("#toast");
    if(!t){ t = document.createElement("div"); t.id="toast"; t.className="toast"; document.body.appendChild(t); }
    t.textContent = msg; t.classList.add("on");
    clearTimeout(t._h); t._h = setTimeout(()=>t.classList.remove("on"), 3200);
  }
  function val(el){
    if(el.type === "checkbox") return el.checked;
    if(el.type === "number") return el.value === "" ? null : Number(el.value);
    return el.value;
  }
  function collect(){
    const out = {};
    document.querySelectorAll("[data-param]").forEach(el=>{
      let v = val(el);
      if(v === null || v === "") return;
      const spec = (wf && wf.params && wf.params[el.dataset.param]) || {};
      if(spec.type === "int") v = Math.round(Number(v));
      if(spec.type === "float") v = Number(v);
      out[el.dataset.param] = v;
    });
    return out;
  }
  function initMask(cv){
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 28; ctx.lineCap = "round";
    let drawing = false, dirty = false;
    const pos = e => {
      const r = cv.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
      const y = (e.touches ? e.touches[0].clientY : e.clientY) - r.top;
      return [x * cv.width / r.width, y * cv.height / r.height];
    };
    const down = e => { drawing = true; dirty = true; const [x,y]=pos(e); ctx.beginPath(); ctx.moveTo(x,y); e.preventDefault(); };
    const move = e => { if(!drawing) return; const [x,y]=pos(e); ctx.lineTo(x,y); ctx.stroke(); e.preventDefault(); };
    const up = () => { drawing = false; };
    ["mousedown","touchstart"].forEach(k=>cv.addEventListener(k,down,{passive:false}));
    ["mousemove","touchmove"].forEach(k=>cv.addEventListener(k,move,{passive:false}));
    ["mouseup","mouseleave","touchend"].forEach(k=>cv.addEventListener(k,up));
    const wrap = cv.closest(".field");
    const size = wrap.querySelector("[data-mask-size]");
    if(size) size.addEventListener("input", ()=>{ ctx.lineWidth = Number(size.value); });
    wrap.querySelector("[data-mask-clear]")?.addEventListener("click", ()=>{
      ctx.fillStyle="#000"; ctx.fillRect(0,0,cv.width,cv.height); dirty=true; });
    wrap.querySelector("[data-mask-invert]")?.addEventListener("click", ()=>{
      const img = ctx.getImageData(0,0,cv.width,cv.height);
      for(let i=0;i<img.data.length;i+=4){ const v = 255 - img.data[i];
        img.data[i]=img.data[i+1]=img.data[i+2]=v; }
      ctx.putImageData(img,0,0); dirty=true; });
    cv._dirty = () => dirty;
    return cv;
  }
  const masks = Array.from(document.querySelectorAll("canvas[data-mask]")).map(initMask);

  function maskBlob(){
    return new Promise(res => {
      const cv = masks.find(c => c._dirty && c._dirty());
      if(!cv){ res(null); return; }
      cv.toBlob(b => res(b), "image/png");
    });
  }

  async function uploadImage(el){
    const f = (el.files || [])[0];
    if(!f) return el.closest(".field").querySelector("[data-param]").value;
    const fd = new FormData();
    fd.append("file", f, f.name);
    const r = await fetch(`${BASE}/api/p2/uploads`, {method:"POST", body: fd});
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || ("HTTP "+r.status));
    const hidden = el.closest(".field").querySelector("[data-param]");
    hidden.value = data.name;
    const pv = el.closest(".field").querySelector("[data-preview]");
    if(pv) pv.innerHTML = `已上传 <code>${esc(data.name)}</code> · ${(data.bytes/1024).toFixed(0)} KB`;
    return data.name;
  }

  function paintCompare(params, artifacts, id){
    if(!params || !params.image || !artifacts.length) return "";
    const orig = `${BASE}/f/inputs/${encodeURIComponent(params.image)}`;
    const out = artifacts[0].url;
    return `<div class="cmp"><img src="${orig}" alt="原图">` +
      `<img class="after" id="cmp-after" src="${out}" alt="结果">` +
      `<span class="lbl a">原图</span><span class="lbl b">结果</span>` +
      `<input type="range" min="0" max="100" value="50" oninput="document.getElementById('cmp-after').style.clipPath='inset(0 0 0 '+this.value+'%)';document.getElementById('cmp-after').style.clipPath='inset(0 0 0 '+this.value+'%)'"></div>`;
  }

  function paint(job){
    const st = job.job || job;
    const id = st.job_id || st.id;
    const artifacts = job.artifacts || [];
    $("#state").innerHTML = `<span class="muted">作业</span> <code>${esc(id||"")}</code> ` +
      `<span class="pill ${/^DONE$/.test(st.state)?"ok":/FAILED|ABORTED/.test(st.state||"")?"bad":"run"}">${esc(st.state||"")}</span>`;
    if(artifacts.length){
      const cmp = paintCompare(st.params, artifacts, id);
      if(cmp) $("#results").insertAdjacentHTML("beforebegin", cmp);
      $("#results").innerHTML = artifacts.map(a =>
        `<figure class="thumb"><a href="${esc(a.url)}" target="_blank">` +
        `<img src="${esc(a.url)}" alt="${esc(a.name)}" loading="lazy"></a>` +
        `<div class="meta"><span>${esc(a.name)}</span><a href="${esc(a.url)}" download>下载</a>` +
        `<a href="${BASE}/app/jobs/${esc(id)}">详情</a></div></figure>`).join("");
    }
  }
  async function poll(id){
    if(Date.now() > deadline){ toast("轮询超时，去任务页看"); return; }
    try{
      const r = await fetch(`${BASE}/api/p2/jobs/${encodeURIComponent(id)}`, {headers:{"Accept":"application/json"}});
      if(!r.ok) throw new Error("HTTP "+r.status);
      const data = await r.json();
      paint(data);
      const st = (data.job||data).state || "";
      if(/^(DONE|FAILED.*|ABORTED)$/.test(st)){ clearInterval(timer); timer=null; toast("作业结束：" + st); return; }
    }catch(e){ /* 网络抖动，下一轮再试 */ }
  }
  function startPoll(id){
    clearInterval(timer);
    deadline = Date.now() + 30*60*1000;
    timer = setInterval(()=>poll(id), 2000);
    poll(id);
  }
  const dl = document.getElementById("dl-selected");
  if(dl) dl.addEventListener("click", ()=>{
    const ids = Array.from(document.querySelectorAll(".job-check:checked")).map(x=>x.value);
    if(!ids.length){ toast("先勾选作业"); return; }
    window.location.href = `${BASE}/api/p2/jobs/zip?ids=` + encodeURIComponent(ids.join(","));
  });
  document.addEventListener("submit", async e=>{
    const form = e.target.closest("#create-form");
    if(!form) return;
    e.preventDefault();
    const btn = $("#submit-btn"); btn.disabled = true;
    let params = collect();
    try{
      const mb = await maskBlob();
      if(mb){
        const fd = new FormData(); fd.append("file", mb, "mask.png");
        const r = await fetch(`${BASE}/api/p2/uploads`, {method:"POST", body: fd});
        const data = await r.json();
        if(!r.ok) throw new Error(data.error || ("HTTP "+r.status));
        const hidden = document.querySelector('[data-param="mask"]');
        if(hidden) hidden.value = data.name;
      }
      for(const el of document.querySelectorAll("[data-upload]")){
        if(el.files && el.files.length) await uploadImage(el);
      }
      params = collect();
    }catch(err){ toast("上传失败：" + err.message); btn.disabled = false; return; }
    const run = $("#run-now").checked;
    try{
      const r = await fetch(`${BASE}/api/p2/jobs`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({workflow: wf.key, params, run})
      });
      const data = await r.json();
      if(!r.ok) throw new Error(data.error || ("HTTP "+r.status));
      toast(run ? "已提交并开始执行" : "已创建作业（未执行）");
      paint({job:{job_id:data.job_id, state:data.state}, artifacts:[]});
      if(run) startPoll(data.job_id);
    }catch(err){ toast("提交失败：" + err.message); }
    finally{ btn.disabled = false; }
  });
})();
"""
