#!/usr/bin/env node
/* 生命周期 UI 单测：长任务勾选 / 三档单选 / 短任务说明 / 提交后提示。
 *
 * 为什么需要：三档的经济含义不一样（60s 打平 / 90s 亏 ¥0.017 / 120s 亏 ¥0.035），
 * 而短任务给 120 秒窗口等于任务成本的 53%~84%。这些数字必须让用户在**提交前**
 * 看见，而不是事后看账单才知道。
 *
 * 最容易坏的地方：**前端复制了一份档位数字**。后端改成 (60, 90, 120) 而前端还写着
 * 90/120，UI 显示的档位就和实际生效的不一致 —— 用户选了 90，跑完留了别的值。
 * 所以这里既测行为，也测"前端没有私自算档位"。
 *
 * 用法：node web/tests/lifecycle_ui.test.js
 *      （在仓库根执行；CI 里由 tools/webui/tests 的 Python 用例顺带调用）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const ROOT = path.join(__dirname, '..', '..');
const APP = path.join(ROOT, 'web', 'dist', 'app.js');
const HTML = path.join(ROOT, 'web', 'dist', 'index.html');
const CSS = path.join(ROOT, 'web', 'dist', 'app.css');
const LIFECYCLE_PY = path.join(ROOT, 'tools', 'p2', 'lifecycle.py');

const src = fs.readFileSync(APP, 'utf8');
const html = fs.readFileSync(HTML, 'utf8');
const css = fs.readFileSync(CSS, 'utf8');
const py = fs.readFileSync(LIFECYCLE_PY, 'utf8');

let pass = 0, fail = 0;
function t(name, body) {
  try { body(); console.log('  ok   ' + name); pass++; }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message); fail++; }
}

/* ---------------------------------------------------------------- 源码区段 */
const START = src.indexOf('/* ==================================================================== 生命周期');
const END = src.indexOf('async function submitJob');
assert(START > 0 && END > START, 'app.js 里找不到生命周期区段');
const lc = src.slice(START, END);
const submitBody = src.slice(END, src.indexOf('/* ==================================================================== 任务列表'));

/* 后端档位（唯一真相） */
const tiers = (py.match(/IDLE_TIERS:\s*tuple\[int, \.\.\.\]\s*=\s*\(([^)]+)\)/) || [, ''])[1]
  .split(',').map(x => parseInt(x.trim(), 10)).filter(Number.isFinite);
const defaultTier = parseInt((py.match(/DEFAULT_LONG_IDLE_S\s*=\s*(\d+)/) || [, ''])[1], 10);

/* ---------------------------------------------------------------- 后端契约 */
t('后端档位是 60/90/120，默认 90', () => {
  assert.deepStrictEqual(tiers, [60, 90, 120], '实际：' + JSON.stringify(tiers));
  assert.strictEqual(defaultTier, 90);
});

/* ---------------------------------------------------------------- 标记 */
t('提交页有「这是长任务」勾选框', () => {
  assert.ok(html.includes('id="isLong"'), '缺 #isLong');
  assert.ok(html.includes('这是长任务'));
});

t('三个档位单选都在', () => {
  for (const v of tiers) {
    assert.ok(html.includes(`name="idleTier" value="${v}"`), `缺 ${v} 秒档`);
  }
});

t('三个档位与后端完全一致，没有多余或缺失', () => {
  const found = [...html.matchAll(/name="idleTier" value="(\d+)"/g)]
    .map(m => parseInt(m[1], 10)).sort((a, b) => a - b);
  assert.deepStrictEqual(found, tiers.slice().sort((a, b) => a - b),
    `HTML 档位 ${JSON.stringify(found)} ≠ 后端 ${JSON.stringify(tiers)}`);
});

t('默认档在三档里，且是后端默认值', () => {
  const m = html.match(/name="idleTier" value="(\d+)" checked/);
  assert.ok(m, '没有任何一档默认勾选');
  assert.strictEqual(parseInt(m[1], 10), defaultTier,
    `UI 默认 ${m[1]} ≠ 后端 ${defaultTier}`);
});

/* ---------------------------------------------------------------- 说明文案 */
t('每一档都有说明（用户要能理解"我在为什么付费"）', () => {
  for (const v of tiers) {
    const re = new RegExp(`value="${v}"[\\s\\S]{0,900}?class="lc-note"`);
    assert.ok(re.test(html), `${v} 秒档缺 .lc-note 说明`);
  }
});

t('90 秒档标了「推荐」', () => {
  assert.ok(/value="90"[^>]*>[\s\S]{0,200}?推荐/.test(html), '90 秒档要标推荐');
});

t('60 秒档说清「打平」', () => {
  const seg = html.slice(html.indexOf('value="60"'), html.indexOf('value="90"'));
  assert.ok(/打平/.test(seg), '60 秒的定位是"数学上打平"');
});

t('120 秒档说清占任务成本的比例', () => {
  const seg = html.slice(html.indexOf('value="120"'));
  assert.ok(/8%/.test(seg), '120 秒要说清占长任务成本的 8%');
});

t('短任务说明常驻（不勾选时用户要能对比）', () => {
  assert.ok(html.includes('id="lcShort"'), '缺 #lcShort');
  const seg = html.slice(html.indexOf('id="lcShort"'),
                         html.indexOf('id="lcShort"') + 400);
  assert.ok(/短任务/.test(seg));
  assert.ok(/立即回收/.test(seg));
  assert.ok(/63%/.test(seg), '要说明"保留 2 分钟等于涨价 63%"');
});

/* ---------------------------------------------------------------- 行为 */
const ESC = `const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));`;

/* readLifecycle：用假 DOM 跑真源码里的函数 */
function runReadLifecycle(isLong, pickedValue) {
  const fnSrc = (lc.match(/const TIER_NOTE = \{[\s\S]*?\n\};/) || [''])[0]
    + '\n' + (lc.match(/function readLifecycle\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
  assert(fnSrc.includes('readLifecycle'), '抠不出 readLifecycle');
  const box = {};
  const fake$ = sel => {
    if (sel === '#isLong') return {checked: isLong};
    return null;
  };
  const doc = {
    querySelector: sel => {
      if (sel === 'input[name=idleTier]:checked') {
        return pickedValue === null ? null : {value: String(pickedValue)};
      }
      return null;
    },
  };
  new Function('module', '$', 'document', ESC + fnSrc + '\nmodule.exports={readLifecycle};')(
    box, fake$, doc);
  return box.exports.readLifecycle();
}

t('不勾长任务 → task_class=short，不带档位', () => {
  const got = runReadLifecycle(false, 120);
  assert.strictEqual(got.task_class, 'short');
  assert.strictEqual(got.idle_tier, null, '短任务不能带档位');
});

t('勾长任务 + 选 60 → 原样送 60', () => {
  const got = runReadLifecycle(true, 60);
  assert.strictEqual(got.task_class, 'long');
  assert.strictEqual(got.idle_tier, 60);
});

t('勾长任务但单选被改坏 → 送 null，由后端取默认', () => {
  const got = runReadLifecycle(true, null);
  assert.strictEqual(got.task_class, 'long');
  assert.strictEqual(got.idle_tier, null, '拿不到档位时交给后端，别在前端瞎猜');
});

t('提交载荷里带了 task_class 和 idle_tier', () => {
  assert.ok(submitBody.includes('task_class'), 'submitJob 没送 task_class');
  assert.ok(submitBody.includes('idle_tier'), 'submitJob 没送 idle_tier');
});

t('前端不自己算档位（数字只有后端一份）', () => {
  /* 允许出现的是 TIER_NOTE 的说明文案和 value 比较，不允许"算出来的档位" */
  const suspicious = [
    /idle_tier\s*[:=]\s*(60|90|120)\b/,     // 硬写档位
    /\?\s*90\s*:/,                          // 三元里冒出一个默认值
  ];
  for (const re of suspicious) {
    assert.ok(!re.test(lc), `前端私自写死了档位：${re}`);
  }
});

t('提交后提示里显示了生命周期结果', () => {
  assert.ok(submitBody.includes('res.lifecycle'), '要读后端返回的 lifecycle');
  assert.ok(submitBody.includes('idle_after_done_s'), '要显示实算的空转秒数');
  assert.ok(submitBody.includes('stopped_ttl_label'), '要显示停机保留多久');
});

t('短任务的提示说「立即回收」而不是留空', () => {
  assert.ok(/立即回收/.test(submitBody), '短任务也要提示，不能什么都不说');
});

/* ---------------------------------------------------------------- 样式 */
t('三档样式存在（.lc-tier / .lc-note / .lc-short）', () => {
  for (const cls of ['.lc-tier', '.lc-name', '.lc-note', '.lc-short', '.lc-foot']) {
    assert.ok(css.includes(cls), `缺样式 ${cls}`);
  }
});

t('选中的档位有视觉区分', () => {
  assert.ok(css.includes('.lc-tier:has(input:checked)'),
    ':has(:checked) 让用户看得出选了哪档');
});

t('档位容器默认隐藏（勾了长任务才展开）', () => {
  const m = html.match(/id="lcTiers"[^>]*hidden/);
  assert.ok(m, '#lcTiers 默认应 hidden');
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
