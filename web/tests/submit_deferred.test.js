#!/usr/bin/env node
/* 提交结果的三态渲染单测：执行 / 降级未执行 / 失败。
 *
 * 为什么需要：并发满时后端会降级成"建作业但不执行"（deferred=true）。
 * 如果前端还是显示"已提交执行"，用户会以为在跑、实际在等 ——
 * 而他不会去点那个"执行"按钮，作业就永远躺在 CREATED。
 * 反过来，如果后端改成 429 直接拒绝，用户刚填好的一整套参数就丢了。
 * 两种都难受，所以必须把"没执行"这件事显示清楚。
 *
 * 用法：node tests/submit_deferred.test.js
 *      （在 web/ 目录下执行；CI 里由 tools/webui/tests 的 Python 用例顺带调用）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const SRC = path.join(__dirname, '..', 'dist', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');

let pass = 0, fail = 0;
function t(name, body) {
  try { body(); console.log('  ok   ' + name); pass++; }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message); fail++; }
}

// 从 submitJob 里抠出渲染分支，用假的 res 跑一遍判断逻辑
const START = src.indexOf('async function submitJob');
const END = src.indexOf('/* ==================================================================== 任务列表');
assert(START > 0 && END > START, 'app.js 里找不到 submitJob 区段');
const body = src.slice(START, END);

/* 从源码里**真正抽出** note 的渲染逻辑，不再手抄一份。
 *
 * 踩过的坑：这里原来是手抄模板 —— 源码把「已创建，未执行」改成
 * 「已排队，未启动」之后，测试还在断言旧文案，于是报"文案没说明没执行"，
 * 而源码其实早就说清楚了。手抄的副本让测试变成**在测它自己**。
 *
 * 做法：从 app.js 截出 `const plan = ...` 到 `const note = ...` 整段，
 * 包成一个工厂函数求值。只依赖两个外部量：`res` 和 `esc`。
 */
const ESC = `const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));`;

const BLK_START = src.indexOf('const plan = res.lifecycle');
const BLK_END = src.indexOf("$('#submitMsg').innerHTML = note;", BLK_START);
assert(BLK_START > 0 && BLK_END > BLK_START,
  'app.js 里找不到 note 渲染区段（`const plan = res.lifecycle` … `innerHTML = note`）');

// 整段原样搬过来，只在末尾追加导出。这样源码怎么改，这里就怎么变。
const BLOCK = src.slice(BLK_START, BLK_END);
const build = new Function('module', 'res', ESC + '\n' + BLOCK + '\nmodule.exports = note;');

function note(res) {
  const m = {};
  build(m, res);
  return m.exports;
}

/* ---------------------------------------------------------------- 源码断言 */
t('submitJob 读 res.deferred', () => {
  assert.ok(body.includes('res.deferred'), '必须读 res.deferred');
});

t('降级时必须明说「没在跑」而不是「已提交」', () => {
  // **断言意图，不锁死文案**。以前这里写的是 assert.ok(includes('已创建，未执行'))，
  // 文案一改成「已排队，未启动」就误报 —— 而那句新文案其实更准确
  // （作业确实在排队，会自动开始）。
  //
  // 真正要守的是这条：用户看完必须知道「现在没在跑」。
  const h = note({job_id: 'p1-x', deferred: true, deferred_reason: '并发已满'});
  assert.ok(/未执行|未启动|排队/.test(h), '必须说出现在没在跑：' + h.slice(0, 120));
  assert.ok(!/已提交执行/.test(h), '不能让人以为已经在执行');
  assert.ok(!h.includes('var(--green)'), '降级不是成功态');
});

t('降级时解释原因（后端给的 deferred_reason）', () => {
  assert.ok(body.includes('res.deferred_reason'), '要显示后端给的原因');
});

t('降级时指路「执行」按钮', () => {
  assert.ok(body.includes('执行'), '要告诉用户怎么启动它');
});

/* ---------------------------------------------------------------- 行为 */
t('deferred=true 渲染成 amber 且不含绿色', () => {
  const h = note({job_id: 'p1-x', state: 'CREATED', deferred: true,
                  deferred_reason: '并发已满：2/2 个作业在跑'});
  assert.ok(h.includes('var(--amber)'), '应当是警示色');
  assert.ok(!h.includes('var(--green)'), '降级不能显示成功绿');
  assert.ok(/未执行|未启动|排队/.test(h), '要说清现在没在跑');
  assert.ok(h.includes('并发已满：2/2'));
});

t('deferred=false 渲染成绿色成功', () => {
  const h = note({job_id: 'p1-x', state: 'PROVISIONING', deferred: false});
  assert.ok(h.includes('var(--green)'));
  assert.ok(h.includes('PROVISIONING'));
  assert.ok(!h.includes('未执行'));
});

t('deferred 缺省（老后端）按已执行处理', () => {
  const h = note({job_id: 'p1-x', state: 'PROVISIONING'});
  assert.ok(h.includes('var(--green)'), '老后端没这个字段时不该误报"未执行"');
});

t('没有 deferred_reason 时给兜底文案', () => {
  const h = note({job_id: 'p1-x', state: 'CREATED', deferred: true});
  assert.ok(h.includes('并发已满'), '要给兜底原因');
});

t('job_id 做 HTML 转义', () => {
  const h = note({job_id: '<b>', state: 'x', deferred: false});
  assert.ok(!h.includes('<b>'), 'job_id 必须转义');
  assert.ok(h.includes('&lt;b&gt;'));
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
