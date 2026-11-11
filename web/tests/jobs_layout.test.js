#!/usr/bin/env node
/* 任务页 KPI + 进行中作业排版：两条都出过真 bug，钉死判据。
 *
 * ## 为什么要这两个测试
 *
 * ① **KPI 数据源不一致**（2026-11-11 用户截图确认）
 *    主数字取 `GET /api/p2/jobs`（35 条），副标题取 `GET /api/p2/queue`
 *    的 `by_state`（只覆盖 8 条）。屏幕上出现「队列总数 35 / cancelled:4 done:4」，
 *    两个数字互相矛盾 —— 用户完全无法判断到底有多少活。
 *    根因不是算错，是**用了两个数据源**。所以判据是：
 *    五项之和必须等于总数，且不许出现来自第二个数据源的 orphan 数字。
 *
 * ② **进行中作业行折行**（同一张截图）
 *    第二行的 job_id 掉到左边缘、提示词和进度条被挤下去。
 *    根因：`.qitem` 的 grid 用了 `1fr`，它隐含 `min-width:auto`，
 *    长工作流名 + 长模型名把整行撑出容器，`.j` 的 `flex-wrap:wrap` 只能折。
 *    修法是 `minmax(0,1fr)` + `flex-wrap:nowrap`。
 *    判据是 CSS 文本 —— 这类 bug 没有断言就会悄悄回来。
 *
 * 用法：node web/tests/jobs_layout.test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const DIST = path.join(__dirname, '..', 'dist');
const appJs = fs.readFileSync(path.join(DIST, 'app.js'), 'utf8');
const appCss = fs.readFileSync(path.join(DIST, 'app.css'), 'utf8');

/* ---------------------------------------------------------------- ① KPI */
/* 只抽 STATE_GROUP —— 它是分组判据的全部。renderJobKpis 摸 DOM，
 * 直接执行会立刻报错，它改用下面的源码级断言来查。 */
const START = appJs.indexOf('const STATE_GROUP');
const END = appJs.indexOf('/* ==================================================================== 顶部状态');
assert(START > 0 && END > START, 'app.js 里找不到 STATE_GROUP 区段');

const sandbox = {};
const fn = new Function('module', 'exports', appJs.slice(START, END) +
  '\nmodule.exports = {STATE_GROUP};');
fn(sandbox, {});
const {STATE_GROUP} = sandbox.exports;

/* 复刻 renderJobKpis 的计算部分，用于断言 —— 直接读它写的 DOM 太脆。 */
function kpiCounts(jobs) {
  const c = {all: jobs.length, run: 0, que: 0, ok: 0, bad: 0, off: 0};
  jobs.forEach(j => { const g = STATE_GROUP(j.state); if (c[g] != null) c[g] += 1; });
  return c;
}

// 真实的 35 条分布（2026-11-11 线上）
const REAL = [
  ...Array(2).fill('RUNNING'),
  ...Array(16).fill('FAILED'),
  ...Array(15).fill('DONE'),
  'FAILED_VERIFY', 'FAILED_RUN',
].map((state, i) => ({job_id: 'p1-x-' + i, state}));

const c = kpiCounts(REAL);

/* 五项必须完整覆盖全部作业，一条不漏一条不重。
 * 这是「两个数据源」那个 bug 的直接判据：只要某项偷偷从别处取数，这里就对不上。 */
assert.strictEqual(c.all, 35, `总数应为 35，实为 ${c.all}`);
assert.strictEqual(
  c.run + c.que + c.ok + c.bad + c.off, c.all,
  `分项之和 ${c.run + c.que + c.ok + c.bad + c.off} ≠ 总数 ${c.all} —— ` +
  `有作业没被归类，用户看到的数字就会自相矛盾`);
assert.strictEqual(c.run, 2);
assert.strictEqual(c.ok, 15);
assert.strictEqual(c.bad, 18, 'FAILED + FAILED_VERIFY + FAILED_RUN 都应算失败');
assert.strictEqual(c.off, 0);

/* 取消不能并进失败 —— 取消是用户主动行为，混进失败会让人以为系统出错了。 */
const withCancelled = [...REAL, ...Array(4).fill('ABORTED').map((s, i) =>
  ({job_id: 'p1-y-' + i, state: s}))];
const c2 = kpiCounts(withCancelled);
assert.strictEqual(c2.off, 4, 'ABORTED 应单独归到 off');
assert.strictEqual(c2.bad, 18, 'ABORTED 不许并进失败');
assert.strictEqual(c2.run + c2.que + c2.ok + c2.bad + c2.off, c2.all);

/* 渲染函数不许再引用 GET /api/p2/queue 的 by_state 当主数字来源。
 * 只允许拿它取「已完成花费」这一个附加信息。 */
const kpiSrc = appJs.slice(appJs.indexOf('function renderJobKpis'),
                           appJs.indexOf('function renderDispatchBar'));
assert(!/by_state[^;]*\.n\b/.test(kpiSrc),
       'renderJobKpis 又在用 queue.by_state 的 n 当数字了 —— 那是第二个数据源');
assert(/S\.jobs\.forEach/.test(kpiSrc),
       'renderJobKpis 必须从 S.jobs 统计，不能从别处取');

/* ---------------------------------------------------------------- ② 折行 */
/* grid 第一列/第二列必须是 minmax(0,1fr)。
 * 单纯 `1fr` 的 min-width 是 auto，长内容会把整行撑出容器。 */
const qitem = appCss.match(/\.qitem\{[^}]*\}/);
assert(qitem, 'app.css 里找不到 .qitem 规则');
assert(/grid-template-columns:[^;]*minmax\(0,\s*1fr\)/.test(qitem[0]),
       '.qitem 的 grid 必须用 minmax(0,1fr)，否则长名字会把行撑出容器导致折行');

const qj = appCss.match(/\.qitem \.j\{[^}]*\}/);
assert(qj, 'app.css 里找不到 .qitem .j 规则');
assert(/flex-wrap:\s*nowrap/.test(qj[0]),
       '.qitem .j 必须 flex-wrap:nowrap —— wrap 会让 job_id 掉到第二行');

/* job_id 不许被压缩/截断：它是唯一标识，折了就看不出是同一条 */
assert(/\.qitem \.j>a\{[^}]*nowrap/.test(appCss) ||
       /\.qitem \.j>a\{[^}]*white-space:\s*nowrap/.test(appCss),
       'job_id 链接必须 white-space:nowrap');

/* 长名字要能省略 + 悬停看全（title）。只省略不提示等于看不全。 */
assert(/\.qitem \.j>\.badge\{[^}]*text-overflow:\s*ellipsis/.test(appCss),
       '工作流名/模型名要 text-overflow:ellipsis');

const renderJobsSrc = appJs.slice(appJs.indexOf('function renderJobs'),
                                  appJs.indexOf('const progressOf'));
const badgeTitles = (renderJobsSrc.match(/title="\$\{esc\(/g) || []).length;
assert(badgeTitles >= 2,
       `进行中作业行至少要有 2 个 title 提示（工作流名、模型名），实为 ${badgeTitles}`);

console.log('✓ jobs_layout: KPI 五项覆盖 %d 条，折行判据全部在位', c.all);
