#!/usr/bin/env node
/* 前端分组/排序逻辑单测：把 app.js 里的纯函数抽出来跑，不依赖浏览器。
 *
 * 为什么需要：工作流分组是本页最重要的信息架构。如果判据写错
 * （比如用名字里有没有「尚未开发」而不是 compat.status），
 * 会把 2 个实际未验证的工作流放进「可用」区，用户点了才发现被 409 拒绝。
 *
 * 用法：node tests/wf_group.test.js
 *      （在 web/ 目录下执行；CI 里由 tools/webui/tests 的 Python 用例顺带调用）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const SRC = path.join(__dirname, '..', 'dist', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');

// 从 app.js 里抠出纯函数区段（WF_GROUPS / isDev / groupWorkflows），
// 不引入整个文件——那会立刻执行 bind()/route() 摸 DOM。
const START = src.indexOf('const WF_GROUPS');
const END = src.indexOf('function wfCard');
assert(START > 0 && END > START, 'app.js 里找不到 WF_GROUPS..wfCard 区段');

const sandbox = {};
const fn = new Function('module', 'exports', src.slice(START, END) +
  '\nmodule.exports = {WF_GROUPS, isDev, groupWorkflows};');
fn(sandbox, {});
const {WF_GROUPS, isDev, groupWorkflows} = sandbox.exports;

let pass = 0, fail = 0;
function t(name, body) {
  try { body(); console.log('  ok   ' + name); pass++; }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message); fail++; }
}

const wf = (key, kind, status, name) => ({
  key, kind, name: name || key, version: 1,
  compat: {status},
});

// ---------------- 分组
t('视频与图片分成两组，顺序为 video→image', () => {
  const g = groupWorkflows([wf('a', 'image', 'verified'), wf('b', 'video', 'verified')]);
  assert.deepStrictEqual(g.map(x => x.kind), ['video', 'image']);
  assert.strictEqual(g[0].title, '视频工作流');
  assert.strictEqual(g[1].title, '图片工作流');
});

t('空组不出现在结果里', () => {
  const g = groupWorkflows([wf('a', 'video', 'verified')]);
  assert.strictEqual(g.length, 1);
  assert.strictEqual(g[0].kind, 'video');
});

// ---------------- 排序：已开发在前
t('verified 排在未开发之前', () => {
  const list = [
    wf('z-todo', 'video', 'experimental'),
    wf('a-done', 'video', 'verified'),
    wf('m-todo', 'video', 'experimental'),
    wf('b-done', 'video', 'verified'),
  ];
  const g = groupWorkflows(list)[0];
  assert.deepStrictEqual(g.done.map(w => w.key), ['a-done', 'b-done']);
  assert.deepStrictEqual(g.todo.map(w => w.key), ['m-todo', 'z-todo']);
  assert.strictEqual(g.total, 4);
});

t('未开发区里不混入 verified（零误分）', () => {
  const list = [
    wf('v1', 'image', 'verified'), wf('e1', 'image', 'experimental'),
    wf('v2', 'image', 'verified'), wf('e2', 'image', 'experimental'),
  ];
  const g = groupWorkflows(list)[0];
  assert(g.done.every(isDev), 'done 区出现了非 verified');
  assert(g.todo.every(w => !isDev(w)), 'todo 区出现了 verified');
});

t('判据用 compat.status 而非名称里的「尚未开发」', () => {
  // 真实数据里有两个 experimental 但名字没标注的，必须也进未开发区
  const list = [
    wf('a', 'image', 'experimental', '普通名字没写尚未开发'),
    wf('b', 'image', 'verified', '某功能（尚未开发）'),   // 名字有标注但实际已验
  ];
  const g = groupWorkflows(list)[0];
  assert.deepStrictEqual(g.done.map(w => w.key), ['b'],
    'names 里含「尚未开发」不代表未开发，应以 compat.status 为准');
  assert.deepStrictEqual(g.todo.map(w => w.key), ['a']);
});

t('同状态内按名称稳定排序（刷新不移位）', () => {
  const list = [
    wf('k1', 'video', 'verified', '丙'),
    wf('k2', 'video', 'verified', '甲'),
    wf('k3', 'video', 'verified', '乙'),
  ];
  const a = groupWorkflows(list)[0].done.map(w => w.key);
  const b = groupWorkflows(list.slice().reverse())[0].done.map(w => w.key);
  assert.deepStrictEqual(a, b, '输入顺序变化后输出顺序应保持一致');
});

// ---------------- 边界
t('compat 缺失按未开发处理，不崩', () => {
  const g = groupWorkflows([{key: 'x', kind: 'video', name: 'x'}])[0];
  assert.strictEqual(g.done.length, 0);
  assert.strictEqual(g.todo.length, 1);
});

t('未知 kind 不落进任何组（不静默归到 image）', () => {
  const g = groupWorkflows([wf('a', 'audio', 'verified'), wf('b', 'image', 'verified')]);
  assert.strictEqual(g.length, 1);
  assert.strictEqual(g[0].kind, 'image');
  assert.strictEqual(g[0].total, 1);
});

t('真实 registry 快照：54 个 / 27+27', () => {
  const snap = path.join(__dirname, 'fixtures', 'workflows.json');
  if (!fs.existsSync(snap)) { console.log('       (跳过：无快照)'); return; }
  const data = JSON.parse(fs.readFileSync(snap, 'utf8'));
  const list = Array.isArray(data) ? data : (data.workflows || []);
  const g = groupWorkflows(list);
  const sum = g.reduce((n, x) => n + x.total, 0);
  assert.strictEqual(sum, list.length, '分组后总数必须等于输入总数');
  for (const grp of g) {
    assert.strictEqual(grp.done.length + grp.todo.length, grp.total, grp.kind + ' 分区数量不符');
  }
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
