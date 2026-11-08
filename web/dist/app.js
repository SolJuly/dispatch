/* 创作控制台 · 前端（对接 docs/openapi.json）
 *
 * 契约要点（与 openapi.json 对齐）：
 *   GET  /api/p2/jobs?limit&state&workflow&q        → {items:[JobSummary], count}
 *   POST /api/p2/jobs  {workflow, params, run, note} → 201 {job_id, state, params, expect…}
 *   GET  /api/p2/jobs/<id>                           → {job, artifacts[], workflow, terminal}
 *   POST /api/p2/jobs/<id>/cancel                    → 仅 CREATED 可取消，执行中 409
 *   POST /api/p2/uploads  (multipart `file`)         → {name, bytes, url}
 *   GET  /api/p2/jobs/zip?ids=a,b                    → application/zip
 *   GET  /api/p2/registry/workflows[/<key>]          → 工作流与**参数契约**
 *   GET  /api/p2/registry/resolve                    → 预演：补默认值/校验/渲染
 *
 * 兼容策略：后端处于迭代中，列表可能有 {items}|{jobs}、工作流可能是数组或对象、
 * 参数契约可能是对象或名字数组——adapter 层统一吸收，页面只吃规范形状。
 */
'use strict';

/* ==================================================================== 基础 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, c, h) => { const n = document.createElement(t); if (c) n.className = c; if (h != null) n.innerHTML = h; return n; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const pad = n => String(n).padStart(2, '0');
const ts = s => { if (!s) return null; const d = new Date(String(s).replace(' ', 'T')); return isNaN(d) ? null : d; };
const hms = d => d ? `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : '—';
const dt = d => d ? `${d.getMonth() + 1}-${pad(d.getDate())} ${hms(d)}` : '—';
const dur = s => { if (s == null || !isFinite(s)) return '—'; s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's'; const m = Math.floor(s / 60), r = s % 60;
  return m < 60 ? `${m}m${r ? pad(r) + 's' : ''}` : `${Math.floor(m / 60)}h${pad(m % 60)}m`; };
const money = v => (v == null || v === '') ? '—' : '¥' + Number(v).toFixed(4);
const bytes = n => n == null ? '—' : n > 1048576 ? (n / 1048576).toFixed(2) + ' MB' : Math.round(n / 1024) + ' KB';

/* token 在路径里（契约约定），如 /files/23e00ec2/ui/ */
const TOKEN = (location.pathname.split('/').filter(Boolean).find(s => /^[0-9a-f]{8,64}$/i.test(s))) || '23e00ec2';
const SAME_BASE = (() => {
  const i = location.pathname.split('/').findIndex(s => s === TOKEN);
  return (i > 0 ? location.pathname.split('/').slice(0, i + 1).join('/') : '') ;
})();

const S = {
  view: 'create', jobId: null,
  baseSel: new URLSearchParams(location.search).get('base') ?? (localStorage.getItem('p2.base') || ''),
  workflows: [], wfKey: null, wfDetail: null, form: {}, uploads: {},
  resolve: null, jobs: [], count: 0,
  filter: {state: 'all', q: '', wf: 'all'},
  selected: new Set(),
  detail: null, detailErr: null,
  queue: null, status: null, dispatch: null,
  auto: true, interval: 5000, lastSync: null, syncMs: 0,
};

/* ==================================================================== 数据层 */
function apiBase() {
  return (S.baseSel || SAME_BASE).replace(/\/$/, '') + '/api/p2';
}
function originOf() {
  if (!S.baseSel) return location.origin;
  try { return new URL(S.baseSel).origin; } catch (e) { return location.origin; }
}
function artUrl(u) {
  if (!u) return '';
  if (/^https?:/.test(u)) return u;
  return originOf() + (u.startsWith('/') ? u : '/' + u);
}

async function get(path, params) {
  const qs = params ? ('?' + new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''))) : '';
  const r = await fetch(apiBase() + path + qs, {cache: 'no-store'});
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    try { const j = await r.json(); msg = j.error || j.detail || msg; } catch (e) { /* keep */ }
    const err = new Error(msg); err.status = r.status; throw err;
  }
  return r.json();
}
async function post(path, body, opts = {}) {
  const r = await fetch(apiBase() + path, {
    method: 'POST', cache: 'no-store',
    headers: opts.form ? {} : {'Content-Type': 'application/json'},
    body: opts.form ? body : JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(data.error || data.detail || `${r.status} ${r.statusText}`);
    err.status = r.status; err.data = data; throw err;
  }
  return data;
}
async function getBlob(path) {
  const r = await fetch(apiBase() + path, {cache: 'no-store'});
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.blob();
}

/* ---------- adapters：吸收后端形状差异 ---------- */
const normList = res => {
  const items = res.items || res.jobs || (Array.isArray(res) ? res : []);
  return {items, count: res.count ?? res.total ?? items.length};
};
const normWorkflows = res => {
  if (Array.isArray(res)) return res;
  if (Array.isArray(res.workflows)) return res.workflows;
  if (res.key) return [res];                 // mock 单对象
  return [];
};
const normArtifacts = (arts, jobId) => (arts || []).map(a => {
  if (typeof a === 'string') {
    return {name: a.split('/').pop(), url: `/pub/jobs/${jobId}/${a.split('/').pop()}`, published: true};
  }
  return {...a, name: a.name || (a.url || '').split('/').pop()};
});
const STATE_GROUP = st => {
  const s = String(st || '').toUpperCase();
  if (s === 'DONE') return 'ok';
  if (s === 'CREATED') return 'que';
  if (s === 'ABORTED') return 'off';
  if (s.startsWith('FAILED')) return 'bad';
  if (/PROVISION|READY|SUBMITTED|RUNNING|FETCHING|VERIFYING|PERSISTED|RELEASING/.test(s)) return 'run';
  if (/QUEUED|CLAIMED/.test(s)) return 'que';
  return 'off';
};
const STATE_CN = {
  CREATED: '已创建', PROVISIONING: '建机中', READY: '实例就绪', SUBMITTED: '已提交',
  RUNNING: '执行中', FETCHING: '拉取产物', VERIFYING: '复验中', PERSISTED: '已落盘',
  RELEASING: '释放实例', DONE: '完成', ABORTED: '已取消',
  FAILED: '失败', FAILED_PROVISION: '建机失败', FAILED_RUN: '执行失败',
  FAILED_FETCH: '拉取失败', FAILED_VERIFY: '复验失败', FAILED_PERSIST: '落盘失败',
};
const stateCn = st => STATE_CN[st] || (String(st || '').startsWith('FAILED') ? '失败(' + st.slice(7) + ')' : (st || '—'));

/* ==================================================================== 顶部状态 */
function setSync(ok, ms, err) {
  const n = $('#sync');
  if (!ok) {
    n.innerHTML = '<i class="live bad"></i><b>连接失败</b>';
    $('#offline').hidden = false; $('#offlineMsg').textContent = err || '';
    return;
  }
  $('#offline').hidden = true;
  n.innerHTML = S.lastSync ? `<i class="live"></i><b>${hms(S.lastSync)}</b> · ${ms}ms`
    : '<i class="live"></i><b>已连接</b>';
}
const markSync = t0 => { S.syncMs = Math.round(performance.now() - t0); S.lastSync = new Date(); setSync(true, S.syncMs); };

let timer = null, busy = false, failN = 0;
function schedule(delay) {
  clearTimeout(timer);
  if (!S.auto || !S.interval || document.hidden) return;
  timer = setTimeout(tick, delay == null ? S.interval : delay);
}
async function tick() {
  if (busy) return schedule();
  busy = true; let next = null;
  try { await refresh(); failN = 0; }
  catch (e) {
    if (!e.status || e.status >= 500) setSync(false, 0, e.message || '');
    failN += 1; next = Math.min(20000, (S.interval || 5000) * 2 ** Math.min(3, failN));
  } finally { busy = false; }
  schedule(next);
}
async function refresh() {
  if (S.view === 'result' && S.jobId) return loadDetail(S.jobId);
  if (S.view === 'create') {
    // 创作台不轮询作业列表，但仍要更新「连接」状态：否则首屏拉完 registry
    // 之后 sync 指示器永远停在「连接中…」，看起来像断了后端。
    const t0 = performance.now();
    try {
      await get('/version');          // 最轻的同源探活（无 token 会 404，不误报）
      markSync(t0);
    } catch (e) {
      if (!e.status || e.status >= 500) setSync(false, 0, e.message || '');
    }
    return;
  }
  const t0 = performance.now();
  const res = normList(await get('/jobs', {limit: 100, state: qsState(), workflow: qsWf(), q: S.filter.q || undefined}));
  S.jobs = res.items; S.count = res.count;
  markSync(t0);
  if (S.view === 'jobs') { await loadQueue(); renderJobs(); }
  if (S.view === 'history') renderHistory();
  if (S.view === 'instances') await loadInstances();
}
const qsState = () => S.filter.state === 'all' ? undefined : S.filter.state;
const qsWf = () => S.filter.wf === 'all' ? undefined : S.filter.wf;

async function loadQueue() {
  try { S.queue = await get('/queue'); } catch (e) { S.queue = null; }
  try { S.dispatch = await get('/dispatch'); } catch (e) { S.dispatch = null; }
}

/* ==================================================================== 创作台 */
async function loadRegistry() {
  try {
    const res = await get('/registry/workflows');
    S.workflows = normWorkflows(res);
  } catch (e) {
    $('#workflows').innerHTML = `<div class="err">registry 加载失败：${esc(e.message)}</div>`;
    return;
  }
  if (!S.workflows.length) { $('#workflows').innerHTML = '<div class="err">registry 为空</div>'; return; }
  const fromHash = new URLSearchParams((location.hash.split('?')[1] || '')).get('workflow');
  if (fromHash && S.workflows.some(w => w.key === fromHash)) S.wfKey = fromHash;
  if (!S.wfKey) S.wfKey = S.workflows[0].key;
  renderWorkflows();
  await selectWorkflow(S.wfKey);
}

/* 工作流分组与排序
 * 分组：video / image 分开（kind 字段，各 27 个）
 * 排序：组内 verified 在前、未开发在后；同状态按名称拼音序，避免顺序漂移
 * 「未开发」判据用 compat.status !== 'verified'，而不是名字里有没有写「尚未开发」——
 * 有 2 个 experimental 的名字没标注，靠名字判会漏。
 */
const WF_GROUPS = [
  {kind: 'video', title: '视频工作流', icon: '🎬'},
  {kind: 'image', title: '图片工作流', icon: '📷'},
];
const isDev = w => ((w.compat || {}).status || '') === 'verified';

function groupWorkflows(list) {
  return WF_GROUPS.map(g => {
    const items = list.filter(w => (w.kind || '') === g.kind);
    // done 在前，未开发在后；稳定排序保证刷新后顺序不变
    const done = items.filter(isDev);
    const todo = items.filter(w => !isDev(w));
    const byName = (a, b) => String(a.name || a.key).localeCompare(String(b.name || b.key), 'zh');
    return {...g, done: done.sort(byName), todo: todo.sort(byName), total: items.length};
  }).filter(g => g.total);
}

function wfCard(w) {
  const st = (w.compat || {}).status || 'unknown';
  const cls = st === 'verified' ? 'pass' : st === 'experimental' ? 'warn' : st === 'incompatible' ? 'fail' : '';
  return `<div class="mcard ${w.key === S.wfKey ? 'on' : ''} ${isDev(w) ? '' : 'undeveloped'}"
       data-key="${esc(w.key)}" title="${esc((w.compat || {}).basis || '')}">
      <div class="t" title="${esc(w.name || w.key)}">${esc(w.name || w.key)}</div>
      <div class="b2"><span class="badge">${esc(w.kind || '')}</span>
        <span class="badge ${cls}">${esc(st)}</span>
        <span class="badge">${esc(w.model_key || w.model || '—')}</span></div>
      <div class="d">${esc(w.key)} · v${esc(w.version || 1)}</div>
    </div>`;
}

function renderWorkflows() {
  const groups = groupWorkflows(S.workflows);
  const doneN = S.workflows.filter(isDev).length;
  $('#wfCount').textContent =
    `${S.workflows.length} 个 · 可用 ${doneN} · 未开发 ${S.workflows.length - doneN}`;
  $('#workflows').innerHTML = groups.map(g => `
    <div class="wfgroup">
      <div class="wfgroup-h">
        <span class="wfgroup-t">${g.icon} ${esc(g.title)}</span>
        <span class="wfgroup-n">${g.done.length} 可用${g.todo.length ? ` · ${g.todo.length} 未开发` : ''}</span>
      </div>
      <div class="models">${g.done.map(wfCard).join('')}</div>
      ${g.todo.length ? `<details class="wfold">
        <summary>未开发 ${g.todo.length} 个（点开查看，默认拒绝执行）</summary>
        <div class="models">${g.todo.map(wfCard).join('')}</div>
      </details>` : ''}
    </div>`).join('');
  if (!$('#workflows').dataset.bound) {
    $('#workflows').addEventListener('click', e => {
      const c = e.target.closest('.mcard'); if (!c) return;
      S.wfKey = c.dataset.key; S.form = {}; S.uploads = {}; S.resolve = null;
      renderWorkflows(); selectWorkflow(S.wfKey);
    });
    $('#workflows').dataset.bound = '1';
  }
}

async function selectWorkflow(key) {
  S.form = {}; S.uploads = {}; S.resolve = null;
  $('#resolveBox').hidden = true;
  $('#formFields').innerHTML = '<div class="loading">读取参数契约…</div>';
  try {
    const res = await get('/registry/workflows/' + encodeURIComponent(key));
    S.wfDetail = res.workflow || res;
  } catch (e) {
    S.wfDetail = null;
    $('#formFields').innerHTML = `<div class="err">参数契约读取失败：${esc(e.message)}
      <br>（mock 可能未覆盖 /registry/workflows/{key}，切到线上再试）</div>`;
    $('#wfSide').innerHTML = '<dd>—</dd>';
    return;
  }
  const params = S.wfDetail.params || {};
  if (Array.isArray(params)) {   // 列表接口只给名字时，退化成无表单
    $('#formFields').innerHTML = `<div class="err">该工作流的参数契约未随列表返回（仅名字：${esc(params.join(', '))}），
      请检查 /registry/workflows/{key} 是否返回 params 对象</div>`;
    return;
  }
  renderForm(params);
  renderSide();
}

function fieldHint(name, spec) {
  const bits = [];
  if (spec.required) bits.push('必填');
  if (spec.min != null || spec.max != null) bits.push(`范围 ${spec.min ?? '−∞'}–${spec.max ?? '+∞'}`);
  if (spec.multiple) bits.push(`×${spec.multiple}`);
  return bits.join(' · ');
}

function renderForm(params) {
  const order = Object.keys(params).sort((a, b) => {
    const w = n => n === 'prompt' ? 0 : n === 'neg_prompt' ? 1 : params[n].type === 'image' ? 2 : 3;
    return w(a) - w(b) || a.localeCompare(b);
  });
  $('#formFields').innerHTML = order.map(name => {
    const spec = params[name] || {};
    const val = S.form[name] !== undefined ? S.form[name] : (spec.default !== undefined ? spec.default : '');
    let ctl;
    if (spec.type === 'image') {
      const up = S.uploads[name];
      ctl = `<div class="up" data-up="${esc(name)}">
        <input type="file" accept="image/png,image/jpeg,image/webp" hidden id="f_${esc(name)}">
        <button class="btn tiny" data-pick="${esc(name)}">选择图片</button>
        <span class="mono upinfo">${up ? esc(up.name) + ' · ' + bytes(up.bytes) : (spec.description || '未上传')}</span>
        ${up ? `<img class="upthumb" src="${esc(artUrl(up.url))}" alt="">` : ''}
      </div>`;
    } else if (spec.type === 'enum') {
      const opts = spec.values || [];
      ctl = `<select name="${esc(name)}">${opts.map(o =>
        `<option value="${esc(o)}" ${String(o) === String(val) ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    } else if (spec.type === 'int' || spec.type === 'float') {
      const step = spec.type === 'float' ? '0.01' : '1';
      ctl = `<input type="number" name="${esc(name)}" value="${esc(val)}" step="${step}"
        ${spec.min != null ? `min="${spec.min}"` : ''} ${spec.max != null ? `max="${spec.max}"` : ''}>
        ${name === 'seed' ? '<button class="btn tiny" data-dice="1">随机</button>' : ''}`;
    } else if (name === 'prompt' || name === 'neg_prompt' || (spec.type === 'string' && (spec.description || '').length > 20)) {
      ctl = `<textarea name="${esc(name)}" placeholder="${esc(spec.description || '')}">${esc(val)}</textarea>`;
    } else {
      ctl = `<input type="text" name="${esc(name)}" value="${esc(val)}">`;
    }
    return `<div class="frow"><label title="${esc(spec.description || '')}">${esc(name)}${spec.required ? ' *' : ''}</label>
      <div class="ctl">${ctl}${fieldHint(name, spec) ? `<span class="fhint">${esc(fieldHint(name, spec))}</span>` : ''}</div></div>`;
  }).join('');

  const box = $('#formFields');
  box.oninput = e => { if (e.target.name) { S.form[e.target.name] = e.target.value; } };
  box.onchange = e => { if (e.target.name) { S.form[e.target.name] = e.target.value; } };
  box.onclick = e => {
    const dice = e.target.closest('[data-dice]');
    if (dice) {
      const v = Math.floor(Math.random() * 2 ** 31);
      S.form.seed = v;
      const inp = box.querySelector('[name="seed"]'); if (inp) inp.value = v;
      return;
    }
    const pick = e.target.closest('[data-pick]');
    if (pick) box.querySelector(`#f_${CSS.escape(pick.dataset.pick)}`).click();
  };
  box.onchange = e => {
    if (e.target.type === 'file') { doUpload(e.target.name.replace(/^f_/, ''), e.target.files[0]); return; }
    if (e.target.name) S.form[e.target.name] = e.target.value;
  };
}

async function doUpload(field, file) {
  if (!file) return;
  const info = field;
  $('#submitMsg').textContent = `上传 ${file.name}…`;
  try {
    const fd = new FormData(); fd.append('file', file);
    const res = await post('/uploads', fd, {form: true});
    S.uploads[info] = res;
    S.form[info] = res.name;
    $('#submitMsg').innerHTML = `<span style="color:var(--green)">已上传 ${esc(res.name)}</span>`;
    renderForm(S.wfDetail.params);
  } catch (e) {
    $('#submitMsg').innerHTML = `<span style="color:var(--red)">上传失败：${esc(e.message)}</span>`;
  }
}

function renderSide() {
  const w = S.wfDetail || {};
  const listed = S.workflows.find(x => x.key === (w.key || S.wfKey)) || {};
  const compat = w.compat || listed.compat || {};
  const lim = w.limits || {};
  $('#wfSide').innerHTML = `
    <dt>工作流</dt><dd>${esc(w.key || S.wfKey)} v${esc(w.version || 1)}</dd>
    <dt>模型</dt><dd>${esc((w.model || {}).key || '—')}</dd>
    <dt>兼容性</dt><dd>${esc(compat.status || '—')} · ${esc(compat.basis || '')}</dd>
    <dt>预算</dt><dd>≤ ${money(lim.max_cost)} · ${lim.max_minutes || '—'} 分钟</dd>
    <dt>参数</dt><dd>${esc(Object.keys(w.params || {}).join(', ') || '—')}</dd>`;
  $('#wfMeta').textContent = `${w.key || ''} v${w.version || 1} · ${w.kind || ''}`;
}

async function doResolve() {
  const params = {...S.form};
  Object.keys(S.uploads).forEach(k => { params[k] = S.uploads[k].name; });
  try {
    const res = await get('/registry/resolve', {workflow: S.wfKey, params: JSON.stringify(params)});
    S.resolve = res;
    $('#resolveBox').hidden = false;
    $('#resolveKv').innerHTML = `
      <dt>模型</dt><dd>${esc(res.model || '—')}</dd>
      <dt>兼容性</dt><dd>${esc((res.compat || {}).status || '—')}</dd>
      <dt>补默认</dt><dd>${esc((res.defaults_applied || []).join(', ') || '无')}</dd>
      <dt>图/帧数</dt><dd>${esc(JSON.stringify(res.expect || {}))}</dd>
      <dt>渲染</dt><dd>${esc((res.render || {}).nodes ?? '—')} 节点 · ${esc(String((res.render || {}).graph_sha256 || '').slice(0, 12))}</dd>`;
    return res;
  } catch (e) {
    $('#resolveBox').hidden = false;
    $('#resolveKv').innerHTML = `<dd style="color:var(--red)">预演失败：${esc(e.message)}</dd>`;
    throw e;
  }
}

async function submitJob() {
  if (!S.wfKey) return;
  const w = S.wfDetail || {};
  const params = {};
  Object.entries((w.params) || {}).forEach(([name, spec]) => {
    let v = S.form[name];
    if (spec.type === 'image') { v = (S.uploads[name] || {}).name; return; }
    if (v === undefined || v === '') {
      if (spec.default !== undefined) v = spec.default;
      else return;                     // 交给服务端补默认 / 报必填
    }
    if (spec.type === 'int') v = parseInt(v, 10);
    if (spec.type === 'float') v = parseFloat(v);
    params[name] = v;
  });
  $('#submitMsg').textContent = '提交中…';
  $('#submit').disabled = true;
  try {
    const res = await post('/jobs', {workflow: S.wfKey, params, run: $('#runNow').checked});
    $('#submitMsg').innerHTML = `<span style="color:var(--green)">${esc(res.job_id)} · ${esc(res.state)}</span>`;
    location.hash = '#/result/' + encodeURIComponent(res.job_id);
  } catch (e) {
    const d = e.data || {};
    const extra = d.error || d.detail || e.message;
    $('#submitMsg').innerHTML = `<span style="color:var(--red)">${esc(extra)}
      ${d.compat ? '（兼容性 ' + esc(d.compat) + '）' : ''}</span>`;
  } finally {
    $('#submit').disabled = false;
  }
}

/* ==================================================================== 任务列表 */
function renderJobs() {
  const active = S.jobs.filter(j => ['run', 'que'].includes(STATE_GROUP(j.state)));
  $('#qnote').textContent = active.length ? `${active.length} 个在跑/排队` : '空闲';
  $('#qempty').hidden = active.length > 0;
  $('#qlist').innerHTML = active.map(j => {
    const g = STATE_GROUP(j.state);
    const pct = progressOf(j);
    return `<div class="qitem">
      <div class="pos dim">${g === 'que' ? '排队' : '跑'}</div>
      <div class="mid">
        <div class="j"><a href="#/result/${encodeURIComponent(j.job_id)}">${esc(j.job_id)}</a>
          <span class="st ${g}"><i class="dot"></i>${esc(stateCn(j.state))}</span>
          <span class="badge">${esc(j.workflow || '')}</span>
          <span class="badge">${esc(j.model || '')}</span></div>
        <div class="meta">${esc((j.prompt || '').slice(0, 80) || '（无 prompt）')} · ${esc(dt(ts(j.created_at)))}</div>
        ${pct != null ? `<div class="pbar"><i style="width:${pct}%"></i></div>` : ''}
      </div>
      <div class="right"><span class="pct">${pct != null ? pct + '%' : '—'}</span>
        <a class="btn tiny" href="#/result/${encodeURIComponent(j.job_id)}">详情</a></div>
    </div>`;
  }).join('');
  renderJobKpis();
  renderDispatchBar(active);
}

const progressOf = j => {
  const st = String(j.state || '');
  const order = ['CREATED', 'PROVISIONING', 'READY', 'SUBMITTED', 'RUNNING', 'FETCHING', 'VERIFYING', 'PERSISTED', 'RELEASING', 'DONE'];
  const i = order.indexOf(st);
  if (st === 'DONE') return 100;
  if (ORDER_FAIL(st)) return null;
  if (i < 0) return null;
  return Math.round((i / (order.length - 1)) * 100);
};
const ORDER_FAIL = st => /^FAILED|^ABORTED/.test(st);

function renderJobKpis() {
  const c = {all: S.count || S.jobs.length, run: 0, que: 0, ok: 0, bad: 0};
  const by = (S.queue && (S.queue.by_state || (S.queue.stats || {}).by_state)) || null;
  S.jobs.forEach(j => { const g = STATE_GROUP(j.state); if (c[g] != null) c[g] += 1; });
  const ks = $('#jkpis').children;
  const set = (i, cls, v, s) => { ks[i].className = 'kpi' + (cls ? ' ' + cls : '');
    ks[i].querySelector('.v').textContent = v; ks[i].querySelector('.s').textContent = s; };
  set(0, '', c.all, by ? Object.entries(by).map(([k, v]) => `${k}:${v.n ?? v}`).join(' ') : 'registry 作业');
  set(1, 'run', c.run, '在建机/执行');
  set(2, 'que', c.que, '等待执行');
  set(3, 'ok', c.ok, 'DONE');
  set(4, 'bad', c.bad, 'FAILED_* / ABORTED');
}

function renderDispatchBar(active) {
  const box = $('#dispatchbar');
  if (!box) return;
  const d = S.dispatch;
  if (!d) { box.hidden = true; return; }
  box.hidden = false;
  if (!d.alive) {
    box.className = 'tip ' + (active.length ? 'bad' : 'warn');
    box.innerHTML = active.length
      ? `⚠ 有 <b>${active.length}</b> 个作业，但调度器没在跑 —— 它们不会自动执行。`
      : '调度器当前没在跑：新作业会停在 CREATED，直到有进程领取。';
  } else {
    box.className = 'tip';
    box.innerHTML = `调度器在线 · 心跳 ${d.age_s != null ? d.age_s + 's 前' : '—'} · 并发 ${d.slots} · 在跑 ${d.running || 0}`;
  }
}

/* ==================================================================== 历史 */
function renderHistory() {
  const tb = $('#jtb');
  $('#jloading').hidden = true;
  $('#jempty').hidden = S.jobs.length > 0;
  $('#jemptyMsg').textContent = '没有符合条件的作业';
  $('#jfoot').textContent = `${S.jobs.length} / ${S.count} 条 · 数据源 ${S.baseSel ? 'Mock' : '线上'}`
    + (S.filter.state !== 'all' ? ` · state=${S.filter.state}` : '')
    + (S.filter.wf !== 'all' ? ` · workflow=${S.filter.wf}` : '')
    + (S.filter.q ? ` · q=${S.filter.q}` : '');
  tb.innerHTML = S.jobs.map(j => {
    const g = STATE_GROUP(j.state);
    const checked = S.selected.has(j.job_id) ? 'checked' : '';
    const art = j.first_artifact_url
      ? `<img class="mini" src="${esc(artUrl(j.first_artifact_url))}" alt="" loading="lazy">` : '';
    return `<tr class="clickable" data-id="${esc(j.job_id)}">
      <td><input type="checkbox" data-sel="${esc(j.job_id)}" ${checked}></td>
      <td><span class="st ${g}"><i class="dot"></i>${esc(stateCn(j.state))}</span></td>
      <td class="mono">${esc(j.workflow || '—')}<span class="badge" style="margin-left:6px">${esc(j.model || '')}</span></td>
      <td class="prompt">${art}<span>${esc((j.prompt || '—').slice(0, 120))}</span></td>
      <td class="mono">${esc(dt(ts(j.created_at)))}</td>
      <td class="num">${j.artifacts ?? 0}</td>
      <td><a class="btn tiny" href="#/result/${encodeURIComponent(j.job_id)}">打开</a></td>
    </tr>`;
  }).join('');
  $('#zip').disabled = S.selected.size === 0;

  tb.onclick = e => {
    const cb = e.target.closest('[data-sel]');
    if (cb) {
      const id = cb.dataset.sel;
      cb.checked ? S.selected.add(id) : S.selected.delete(id);
      $('#zip').disabled = S.selected.size === 0;
      return;
    }
    if (e.target.closest('a')) return;
    const tr = e.target.closest('tr[data-id]');
    if (tr) location.hash = '#/result/' + encodeURIComponent(tr.dataset.id);
  };
}

/* ==================================================================== 结果 */
async function loadDetail(id) {
  const t0 = performance.now();
  try {
    const res = await get('/jobs/' + encodeURIComponent(id));
    S.detail = res; S.detailErr = null; markSync(t0);
  } catch (e) {
    S.detailErr = e;
    if (!e.status || e.status >= 500) setSync(false, 0, e.message || '');
  }
  renderResult();
}

function renderResult() {
  const root = $('#result');
  if (S.detailErr) {
    const nf = S.detailErr.status === 404;
    root.innerHTML = `<div class="card"><div class="empty"><span class="big">${nf ? '∅' : '⚠'}</span>
      ${nf ? '作业不存在' : '读取失败'}<br><span class="mono" style="color:var(--dim)">${esc(S.jobId)} · ${esc(S.detailErr.message)}</span>
      <div style="margin-top:14px"><a class="btn" href="#/history">← 历史</a><a class="btn p" href="#/create">去创作台</a></div>
      </div></div>`;
    return;
  }
  if (!S.detail) { root.innerHTML = '<div class="card"><div class="loading">加载中…</div></div>'; return; }
  const job = S.detail.job || {};
  const arts = normArtifacts(S.detail.artifacts, job.job_id);
  const g = STATE_GROUP(job.state);
  const terminal = S.detail.terminal ?? (['DONE', 'ABORTED'].includes(job.state) || ORDER_FAIL(job.state));
  const wf = S.detail.workflow || (job.model && job.model.registry && job.model.registry.workflow_key) || '';
  const steps = job.steps || {};
  const params = job.params || {};

  root.innerHTML = `
    <div class="res-head">
      <div><h2>${esc(job.job_id || S.jobId)}</h2>
        <div class="sub">${esc(wf)} · ${esc(job.kind || '')} · 创建 ${esc(dt(ts(job.created_at)))}
          ${terminal ? '' : '· 轮询中'}</div></div>
      <div class="acts">
        <span class="st ${g}"><i class="dot"></i>${esc(stateCn(job.state))}</span>
        ${job.params_hash ? `<span class="badge">${esc(String(job.params_hash).slice(7, 17))}…</span>` : ''}
        ${job.state === 'CREATED' ? '<button class="btn danger" id="btnCancel">取消作业</button>' : ''}
        ${arts.length ? '<button class="btn" id="btnZip">打包下载</button>' : ''}
        <a class="btn" href="#/history">← 历史</a>
      </div>
    </div>
    ${ORDER_FAIL(job.state) ? `<div class="tip bad">作业失败：${esc(job.state)}${job.error ? ' · ' + esc(job.error) : ''}</div>`
      : job.state === 'DONE' ? '<div class="tip">完成 · 产物已发布（URL 与进程无关，可长期访问）</div>' : ''}
    ${arts.length ? `<div class="media">${arts.map(a => artifactCard(a, job.job_id)).join('')}</div>`
      : `<div class="card"><div class="empty"><span class="big">▢</span>
          ${terminal ? '没有产物' : '还没产物，页面会自动刷新'}</div></div>`}
    <div class="meta-grid">
      <div class="card"><h3>参数快照 <span class="r">${esc(wf)}</span></h3>
        <div class="pad"><dl class="kv">
          ${Object.entries(params).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join('') || '<dd>—</dd>'}
        </dl></div></div>
      <div class="card"><h3>步骤 <span class="r">state_seq ${esc(job.state_seq ?? '—')}</span></h3>
        <div class="pad"><div class="gantt">
          ${Object.entries(steps).map(([k, v]) => `<div class="g"><div class="lab">${esc(k)}</div>
            <div class="track"><div class="fill" style="width:${v && v.done ? 100 : 8}%"></div></div>
            <div class="val">${esc(v && v.at ? hms(ts(v.at)) : '—')}</div></div>`).join('') || '<div class="note">无步骤记录</div>'}
        </div></div></div>
    </div>`;

  $('#btnCancel') && $('#btnCancel').addEventListener('click', () => cancelJob(job.job_id));
  $('#btnZip') && $('#btnZip').addEventListener('click', () => downloadZip([job.job_id]));
}

function artifactCard(a, jobId) {
  const url = esc(artUrl(a.url));
  const isVid = /\.(mp4|webm|mov)$/i.test(a.name || '');
  const isImg = /\.(png|jpe?g|webp|gif)$/i.test(a.name || '');
  const body = isVid ? `<video controls preload="metadata" playsinline src="${url}"></video>`
    : isImg ? `<img src="${url}" alt="${esc(a.name)}" loading="lazy">`
    : `<div class="pad mono">${esc(a.name)}</div>`;
  return `<div class="m1">${body}
    <div class="cap"><span>${esc(a.name)} · ${bytes(a.bytes)}${a.width ? ` · ${a.width}×${a.height}` : ''}
      ${a.published === false ? ' · <span style="color:var(--amber)">发布中</span>' : ''}</span>
      <span class="r"><a class="btn tiny" href="${url}" target="_blank" rel="noopener">原文件</a>
      <a class="btn tiny p" href="${url}" download>下载</a></span></div></div>`;
}

async function cancelJob(id) {
  if (!confirm(`取消作业 ${id}？\n\n只有「已创建（CREATED）」可以取消；已开始执行会返回 409。`)) return;
  try {
    await post(`/jobs/${encodeURIComponent(id)}/cancel`, {});
    await loadDetail(id);
  } catch (e) {
    alert('取消失败：' + (e.message || e) + (e.status === 409 ? '\n（已开始执行，需要用 run.py --abort）' : ''));
  }
}

async function downloadZip(ids) {
  try {
    const blob = await getBlob('/jobs/zip?ids=' + encodeURIComponent(ids.join(',')));
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `jobs-${ids.length}.zip`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) {
    alert('打包下载失败：' + (e.message || e));
  }
}

/* ==================================================================== 运行环境 */
async function loadInstances() {
  try { S.queue = await get('/queue'); } catch (e) { S.queue = null; }
  try { S.dispatch = await get('/dispatch'); } catch (e) { S.dispatch = null; }
  let inst = null;
  try { inst = await get('/instances'); } catch (e) { inst = null; }
  const rows = (inst && inst.instances) || [];
  const stats = (inst && inst.stats) || {total: rows.length};
  const ks = $('#ikpis').children;
  const set = (i, cls, v, s) => { ks[i].className = 'kpi' + (cls ? ' ' + cls : '');
    ks[i].querySelector('.v').textContent = v; ks[i].querySelector('.s').textContent = s; };
  set(0, '', stats.total ?? 0, inst ? `窗口 ${Math.round((inst.window_s || 0) / 3600)}h` : '接口不可用');
  set(1, 'run', stats.running ?? 0, '有活跃作业');
  set(2, '', stats.idle ?? rows.filter(r => r.state === 'idle').length, '在线空闲');
  set(3, 'rec', stats.reclaimed ?? 0, 'watchdog');
  set(4, '', money(stats.cost_cny_sum), '近窗花费');

  $('#igrid').innerHTML = rows.length ? rows.map(r => {
    const g = r.state === 'running' ? 'run' : r.state === 'reclaimed' ? 'rec' : 'off';
    const cn = r.state === 'running' ? '运行中' : r.state === 'idle' ? '在线空闲'
      : r.state === 'reclaimed' ? '已回收' : r.state === 'offline' ? '离线' : '未知';
    return `<div class="inst ${r.state === 'running' ? 'run' : ''}">
      <div class="top2"><span class="st ${g}"><i class="dot"></i>${cn}</span>
        <span class="id" title="${esc(r.instance_id)}">${esc(r.instance_id)}</span>
        ${r.name ? `<span class="nm">${esc(r.name)}</span>` : ''}</div>
      <div class="ln">当前任务 <b>${r.running_job_id
        ? `<a class="joblink" href="#/result/${encodeURIComponent(r.running_job_id)}">${esc(r.running_job_id)}</a>` : '—'}</b></div>
      <div class="ln">累计花费 <b>${money(r.cost_cny)}</b><span class="r">${r.samples ?? 0} 次采样</span></div>
      ${r.gpu_util != null ? `<div class="bars"><div class="bar1"><span>GPU</span>
        <div class="t"><div class="f" style="width:${Math.min(100, r.gpu_util)}%"></div></div><span>${r.gpu_util}%</span></div></div>` : ''}
    </div>`;
  }).join('') : `<div class="card" style="grid-column:1/-1"><div class="empty"><span class="big">▢</span>
      实例接口不可用或窗口内没有实例<br><span class="mono" style="color:var(--dim)">/api/p2/instances 属于本工作区扩展接口，线上后端若未合并则为 404</span></div></div>`;

  const wd = (inst && inst.watchdog) || [];
  $('#wdn').textContent = wd.length + ' 条';
  $('#wdb').innerHTML = wd.length ? wd.map(w => `<tr><td class="mono">${esc(w.at || '—')}</td>
      <td class="mono">${esc(w.instance_id || '—')}</td>
      <td class="mono">${esc(w.job_id || '—')}</td><td>${esc(w.reason || '')}</td>
      <td><span class="badge warn">${esc(w.action || '')}</span></td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">窗口内没有回收记录</td></tr>';
  $('#wdnote').textContent = inst ? (inst.notes || []).join(' · ') : '接口不可用';
  $('#wdfoot').innerHTML = `<div class="note">数据来源：<span class="mono">/api/p2/instances</span>（本工作区扩展）
    与 <span class="mono">/api/p2/status</span>；实例采样来自 <span class="mono">.compshare/watch.log</span>。</div>`;
}

/* ==================================================================== 路由 */
function parseHash() {
  const raw = (location.hash || '#/create').replace(/^#/, '');
  const [path, query] = raw.split('?');
  const seg = path.split('/').filter(Boolean);
  if (seg[0] === 'result' && seg[1]) return {view: 'result', id: decodeURIComponent(seg[1])};
  if (['create', 'jobs', 'history', 'instances'].includes(seg[0])) return {view: seg[0]};
  return {view: 'create'};
}

async function route() {
  const r = parseHash();
  S.view = r.view; S.jobId = r.id || null;
  ['create', 'jobs', 'result', 'history', 'instances'].forEach(v =>
    $('#view-' + v).classList.toggle('on', v === S.view));
  const nav = {create: 'create', jobs: 'jobs', history: 'history', instances: 'instances', result: 'history'};
  $$('#tabs a').forEach(a => a.classList.toggle('on', a.dataset.nav === nav[S.view]));

  if (S.view === 'create') {
    if (!S.workflows.length) await loadRegistry();
    // 创作台不轮询作业列表，但要走一次 tick 把「连接」状态点亮并维持后续调度；
    // 否则 sync 指示器永远停在「连接中…」，看起来像后端断了。
    try { await tick(); } catch (e) { /* 顶栏已提示 */ }
    return;
  }
  if (S.view === 'result') {
    S.detail = null; S.detailErr = null;
    $('#result').innerHTML = '<div class="card"><div class="loading">加载中…</div></div>';
    await loadDetail(S.jobId); schedule(0); return;
  }
  if (S.view === 'history') $('#jloading').hidden = false;
  try { await tick(); } catch (e) { /* 顶栏已提示 */ }
}

/* ==================================================================== 事件 */
function bind() {
  $('#auto').checked = S.auto;
  $('#interval').value = String(S.interval);
  $('#base').value = S.baseSel;
  $('#base').addEventListener('change', e => {
    S.baseSel = e.target.value;
    localStorage.setItem('p2.base', S.baseSel);
    S.workflows = []; S.wfKey = null; S.jobs = []; S.queue = null; S.dispatch = null;
    if (S.view === 'create') loadRegistry().then(() => {}); else tick();
  });
  $('#auto').addEventListener('change', e => { S.auto = e.target.checked; schedule(0); });
  $('#interval').addEventListener('change', e => {
    S.interval = Number(e.target.value); if (!S.interval) S.auto = false;
    $('#auto').checked = S.auto; schedule(0);
  });
  $('#retry').addEventListener('click', () => { failN = 0; schedule(0); });
  $('#resolve').addEventListener('click', () => doResolve().catch(() => {}));
  $('#submit').addEventListener('click', submitJob);
  $('#reset').addEventListener('click', () => {
    S.filter = {state: 'all', q: '', wf: 'all'}; S.selected.clear();
    $('#q').value = ''; $('#wfFilter').value = 'all'; syncChips(); renderHistory();
  });
  $('#q').addEventListener('input', e => { S.filter.q = e.target.value; });
  $('#q').addEventListener('change', () => tick());
  $('#wfFilter').addEventListener('change', e => { S.filter.wf = e.target.value; tick(); });
  $('#zip').addEventListener('click', () => downloadZip([...S.selected]));
  $('#checkAll').addEventListener('change', e => {
    S.selected = e.target.checked ? new Set(S.jobs.map(j => j.job_id)) : new Set();
    renderHistory();
  });
  $('#dclose').addEventListener('click', () => { $('#drawer').hidden = true; });
  $('#dcopy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#dlog').textContent); $('#dcopy').textContent = '已复制'; }
    catch (e) { $('#dcopy').textContent = '失败'; }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer); else schedule(0);
  });
  window.addEventListener('hashchange', route);
  setInterval(() => { /* 本地计时只刷新耗时文本，不发请求 */ }, 1000);
}

/* 状态 chip */
const STATE_FILTERS = [
  ['all', '全部'], ['RUNNING', '执行中'], ['CREATED', '已创建'],
  ['DONE', '完成'], ['FAILED', '失败'], ['ABORTED', '已取消'],
];
function syncChips() {
  const box = $('#chips');
  if (!box.dataset.ready) {
    box.innerHTML = STATE_FILTERS.map(([k, n]) =>
      `<div class="chip" data-k="${k}">${n}</div>`).join('');
    box.addEventListener('click', e => {
      const c = e.target.closest('.chip'); if (!c) return;
      S.filter.state = c.dataset.k; syncChips(); tick();
    });
    box.dataset.ready = '1';
  }
  $$('#chips .chip').forEach(c => c.classList.toggle('on', c.dataset.k === S.filter.state));
  const sel = $('#wfFilter');
  const keys = S.workflows.map(w => w.key);
  const sig = keys.join(',');
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = '<option value="all">全部工作流</option>' +
      keys.map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
    sel.dataset.sig = sig;
  }
  sel.value = keys.includes(S.filter.wf) ? S.filter.wf : 'all';
}

/* ==================================================================== 启动 */
bind();
syncChips();
if (!location.hash) location.hash = '#/create';
if (!S.workflows.length) loadRegistry();
route();
