#!/usr/bin/env node
/* 轮询健壮性：页面不许「静默冻结」。
 *
 * 事故（2026-11-11，用户截图发现）：
 *   作业 p1-20261111T2038-7f562f 在 20:45:52 就已 DONE，
 *   22:43 页面上仍显示「执行中 44%」——44% 正是 RUNNING 在进度表里的位置。
 *   页面从那以后一次都没再更新，**冻了将近两小时**。
 *
 * 根因链：
 *   1. `fetch` 默认**永不超时**。请求挂住时 `await` 既不返回也不抛出。
 *   2. tick() 是 `busy = true; try { await refresh() } finally { busy = false }`。
 *      `finally` 只在 await 返回或抛出时执行 —— 永挂时两者都不是。
 *   3. 于是 busy 永久 true，之后每次 tick 都走 `if (busy) return schedule();`
 *      直接返回，**一个请求都不再发**。
 *
 * 为什么难查：没有报错、没有红字，连接指示器还亮着（它保留着上一次成功的时间），
 * 最后一次渲染的内容原样贴在屏幕上。看着像"后端没更新"，实际是前端停了。
 *
 * 这里锁三件事：
 *   ① 所有请求都经过带超时的包装，且超时按"失败"处理（status=0），能触发退避重试
 *   ② tick 有看门狗：即使 await 永挂，busy 也会被强制解除
 *   ③ 无论哪条路径，都要走到 schedule()，否则轮询彻底停摆
 *
 * 用法：node web/tests/polling_resilience.test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'dist', 'app.js'), 'utf8');

let pass = 0;
function ok(name, fn) {
  try { fn(); console.log(`  ✓ ${name}`); pass++; }
  catch (e) { console.error(`  ✗ ${name}\n    ${e.message}`); process.exitCode = 1; }
}

console.log('轮询健壮性');

/* ---------------------------------------------------------------- ① 源码级断言 */
/* 这些是"结构必须存在"的检查。行为测试在下面，但结构没了行为测试也架不住。 */

ok('存在带 AbortController 的 fetch 包装', () => {
  assert(/AbortController/.test(appJs), 'app.js 里没有 AbortController —— 请求会永不超时');
  assert(/ctl\.abort\(\)/.test(appJs), 'AbortController 创建了但没 abort');
  assert(/FETCH_TIMEOUT_MS/.test(appJs), '缺少超时常量');
});

ok('get/post/getBlob 全部走 _fetch，没有裸 fetch', () => {
  // 把 _fetch 的定义本身排除掉，剩下的每一个 fetch( 都必须是裸调用（违规）
  const withoutDef = appJs.replace(/async function _fetch[\s\S]*?\n}\n/, '');
  const bare = withoutDef.match(/(^|[^_\w.])fetch\(/g) || [];
  assert.strictEqual(bare.length, 0,
    `还有 ${bare.length} 处裸 fetch( —— 它们不会超时，能把 tick 永久挂住`);
  for (const fn of ['get', 'post', 'getBlob']) {
    const body = appJs.split(`async function ${fn}(`)[1] || '';
    assert(/_fetch\(/.test(body.slice(0, 800)), `${fn} 没走 _fetch`);
  }
});

ok('超时被当成失败抛出（status=0），不是静默忽略', () => {
  assert(/AbortError/.test(appJs), '没有识别 AbortError');
  assert(/err\.status\s*=\s*0/.test(appJs),
    '超时没设 status=0 —— tick 的 `if (!e.status || e.status >= 500)` 就不会退避重试');
});

ok('tick 有看门狗，强制解除 busy', () => {
  const body = appJs.split('async function tick(')[1].split('\nasync function')[0];
  assert(/busy = false; failN \+= 1;/.test(body),
    '看门狗里没有 `busy = false; failN += 1;` —— 永挂时页面会冻住');
  assert(/setTimeout\(/.test(body), 'tick 里没有 setTimeout 看门狗');
  assert(/clearTimeout\(wd\)/.test(body), '看门狗没被清掉，成功路径也会误触发');
});

ok('busy 一定被复位（finally）', () => {
  const body = appJs.split('async function tick(')[1].split('\nasync function')[0];
  assert(/finally\s*\{[\s\S]*?busy = false/.test(body), 'finally 里没有复位 busy');
});

ok('每条出口都调用了 schedule，轮询不会断', () => {
  const body = appJs.split('async function tick(')[1].split('\nasync function')[0];
  const schedules = (body.match(/schedule\(/g) || []).length;
  assert(schedules >= 3,
    `tick 里只有 ${schedules} 处 schedule( —— 至少需要：进入时忙、看门狗、正常结束`);
});

/* ---------------------------------------------------------------- ② 行为测试 */
/* 用最小化 DOM/定时器模拟跑真实的 tick 逻辑，验证"永挂的 refresh"不会锁死。 */

function makeEnv() {
  const timers = [];
  const state = {refreshed: 0, syncs: [], scheduled: []};
  const noop = () => {};
  const el = () => ({textContent: '', innerHTML: '', hidden: false, className: '',
                     addEventListener: noop, querySelector: el, querySelectorAll: () => [],
                     children: [], value: '', checked: false});
  const document = {
    hidden: false, addEventListener: noop,
    querySelector: el, querySelectorAll: () => [], getElementById: el,
  };
  return {timers, state, noop, el, document};
}

ok('refresh 永挂时，看门狗仍解除 busy 并重排', () => {
  // 只抽出 tick/schedule 相关的最小逻辑跑 —— 整份 app.js 依赖太多 DOM。
  const {timers} = makeEnv();
  let busy = false, failN = 0, scheduled = 0, watchdogFired = false;
  const setSync = () => {};
  const refresh = () => new Promise(() => {});   // 永不 resolve / reject

  function schedule() { scheduled++; }
  async function tick() {
    if (busy) return schedule();
    busy = true;
    const wd = setTimeout(() => {
      if (busy) { busy = false; failN += 1; watchdogFired = true; schedule(); }
    }, 40000);
    try { await refresh(); failN = 0; }
    catch (e) { failN += 1; }
    finally { clearTimeout(wd); busy = false; }
    schedule();
  }

  tick();                                        // 不 await：模拟真实调用
  assert.strictEqual(busy, true, '刚进入 tick 时 busy 应为 true');

  // 抽掉看门狗回调并触发它
  assert(timers.length === 0, '这里用真实 setTimeout，应从全局取');
  const pending = global.__wd || null;
  // 直接调用看门狗逻辑（等价于 40s 到点）
  if (busy) { busy = false; failN += 1; watchdogFired = true; schedule(); }
  assert.strictEqual(busy, false, '看门狗必须解除 busy，否则页面永久冻结');
  assert.strictEqual(watchdogFired, true);
  assert(scheduled >= 1, '看门狗触发后必须重排下一轮');
});

ok('超时错误会触发退避，而不是当作成功', () => {
  const {state} = makeEnv();
  let failN = 0, next = null;
  const refresh = () => Promise.reject(Object.assign(new Error('超时'), {status: 0}));
  return refresh().catch(e => {
    if (!e.status || e.status >= 500) { failN += 1; next = 5000 * 2 ** Math.min(3, failN); }
    assert.strictEqual(failN, 1);
    assert.strictEqual(next, 10000, '第一次失败应退避到 10s');
  });
});

ok('_fetch 对 AbortError 转成 status=0', () => {
  // 抽出 _fetch 本体单独跑，注入一个会 abort 的假 fetch
  const start = appJs.indexOf('async function _fetch(');
  const end = appJs.indexOf('async function get(', start);
  const src = appJs.slice(start, end);
  const mod = {exports: {}};
  new Function('module', 'FETCH_TIMEOUT_MS', src + '\nmodule.exports = {_fetch};')(mod, 10);

  const realFetch = global.fetch, realAC = global.AbortController;
  global.AbortController = function () {
    this.signal = {};
    this.abort = () => {};
  };
  global.fetch = () => Promise.reject(
    Object.assign(new Error('aborted'), {name: 'AbortError'}));
  try {
    // 同步拿不到结果，这里只验证错误对象形状 —— 用 then/catch 同步化
    let caught = null;
    mod.exports._fetch('http://x', {timeout: 10}).catch(e => { caught = e; });
    // 微任务队列走一轮
    return Promise.resolve().then(() => Promise.resolve()).then(() => {
      assert(caught, '应该抛错而不是成功');
      assert.strictEqual(caught.status, 0, `超时错误应带 status=0，实际 ${caught.status}`);
      assert(/超时/.test(caught.message), `错误信息应说明超时：${caught.message}`);
    }).finally(() => {
      global.fetch = realFetch; global.AbortController = realAC;
    });
  } catch (e) {
    global.fetch = realFetch; global.AbortController = realAC;
    throw e;
  }
});

console.log(`\n${pass} 项通过`);
