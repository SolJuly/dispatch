#!/usr/bin/env node
/* 前端质量卡单测：真的把 qualityCard() 跑起来看它吐出什么 HTML。
 *
 * 为什么需要：
 *   1. 「没有 quality 数据时整张卡不渲染」是个负向断言，只 grep 源码看不出来 ——
 *      必须真的传 null / {} / 缺字段跑一遍。
 *   2. 等级徽章的颜色分档（A/B/C/D）如果写错，界面会变成"32 分 D 级也是绿的"，
 *      而那正是这张卡存在的理由。
 *   3. issues 必须原文显示（"边缘能量 0.56 < 0.8：画面糊/抽象"）——
 *      report 模式不拦截，用户能看到的就只有这些字。
 *
 * 用法：node tests/quality_card.test.js
 *      （在 web/ 目录下执行；CI 里由 tools/webui/tests 的 Python 用例顺带调用）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const SRC = path.join(__dirname, '..', 'dist', 'app.js');
const src = fs.readFileSync(SRC, 'utf8');

// 只抠质量卡区段（QGRADE..renderResult），不引入整个文件 ——
// 那会立刻执行 bind()/route() 去摸 DOM。esc() 在这里复刻一份：
// 它是纯函数，行为由 tools/webui/tests 的既有用例守着。
const START = src.indexOf('const QGRADE');
const END = src.indexOf('function renderResult');
assert(START > 0 && END > START, 'app.js 里找不到 QGRADE..renderResult 区段');

const ESC = `const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));`;

const sandbox = {};
const fn = new Function('module', ESC + '\n' + src.slice(START, END) +
  '\nmodule.exports = {QGRADE, qualityCard};');
fn(sandbox, {});
const {QGRADE, qualityCard} = sandbox.exports;

let pass = 0, fail = 0;
function t(name, body) {
  try { body(); console.log('  ok   ' + name); pass++; }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message); fail++; }
}

// 真实形状：tools/p2/quality.py QualityReport.as_dict()
const rep = (over = {}) => Object.assign({
  mode: 'report', overall: 87.3, grade: 'A', verdict: 'PASS',
  artifacts: [{
    file: '00-primary.mp4', kind: 'video', score: 87.3, grade: 'A',
    metrics: {sharpness: 2.783, motion: 36.105, luma_mean: 117.7},
    issues: [], gated: false, passed: true,
  }],
}, over);

/* ---------------------------------------------------------------- 不渲染 */
t('null 不渲染', () => assert.strictEqual(qualityCard(null), ''));
t('undefined 不渲染', () => assert.strictEqual(qualityCard(undefined), ''));
t('空对象不渲染（老 manifest 写的 quality={}）',
  () => assert.strictEqual(qualityCard({}), ''));
t('缺 overall 不渲染（只有 error 字段的评分失败）',
  () => assert.strictEqual(qualityCard({error: 'TimeoutExpired: x'}), ''));
t('overall 非数字不渲染', () => assert.strictEqual(qualityCard({overall: 'n/a'}), ''));
t('字符串当 quality 传进来不渲染', () => assert.strictEqual(qualityCard('A'), ''));

/* ---------------------------------------------------------------- 渲染 */
t('渲染出带 qcard 的卡片', () => {
  const h = qualityCard(rep());
  assert.ok(h.includes('class="card qcard"'), '应带 .card.qcard');
  assert.ok(h.includes('产物质量'), '应有标题');
});

t('A 级：徽章与分数都是 a 档', () => {
  const h = qualityCard(rep());
  assert.ok(h.includes('qbadge a'), '徽章应为 a 档');
  assert.ok(h.includes('qscore a'), '分数应为 a 档');
  assert.ok(/<span class="qscore a">87\.3<i>\/100<\/i>/.test(h), '分数应保留一位小数并带 /100');
});

t('D 级不能被渲染成绿色', () => {
  const h = qualityCard(rep({
    overall: 32.0, grade: 'D',
    artifacts: [{file: 'x.mp4', score: 32, grade: 'D', issues: ['边缘能量 0.56 < 0.8：画面糊/抽象']}],
  }));
  assert.ok(h.includes('qbadge d'), 'D 级徽章应为 d 档');
  assert.ok(!h.includes('qbadge a'), 'D 级不能出现 a 档徽章');
  assert.ok(h.includes('32.0'), '分数应显示');
});

t('B / C 各自落在 b / c 档', () => {
  assert.ok(qualityCard(rep({overall: 60, grade: 'B'})).includes('qbadge b'));
  assert.ok(qualityCard(rep({overall: 40, grade: 'C'})).includes('qbadge c'));
});

t('未知等级降级成 x 档而不是崩掉', () => {
  const h = qualityCard(rep({overall: 50, grade: '?'}));
  assert.ok(h.includes('qbadge x'), '应为兜底档');
  assert.ok(h.includes('未知'), '应给出"未知"字样');
});

t('issues 原文显示（report 模式下用户只能看见这行字）', () => {
  const issue = '边缘能量 0.56 < 0.8：画面糊/抽象';
  const h = qualityCard(rep({
    overall: 32, grade: 'D',
    artifacts: [{file: 'x.mp4', score: 32, grade: 'D', issues: [issue]}],
  }));
  assert.ok(h.includes('qissues'), '应有 issues 列表');
  assert.ok(h.includes('<li>'), 'issues 应为列表项');
  assert.ok(h.includes('边缘能量 0.56 &lt; 0.8：画面糊/抽象'), 'issues 要转义后原样出现');
});

t('一句人话结论取第一个 issue', () => {
  const h = qualityCard(rep({
    overall: 32, grade: 'D',
    artifacts: [{file: 'x.mp4', score: 32, grade: 'D', issues: ['帧差 0.10 < 0.6：画面基本没动']}],
  }));
  assert.ok(/class="qsay">[^<]*帧差 0\.10/.test(h), 'qsay 里应点名第一个问题');
});

t('没有 issues 时给"全部达标"而不是空列表', () => {
  const h = qualityCard(rep());
  assert.ok(h.includes('qnone'), '应渲染 qnone');
  assert.ok(!h.includes('qissues'), '不应有空的问题列表');
});

t('report 模式标明"仅记录，不影响作业结论"', () => {
  const h = qualityCard(rep());
  assert.ok(h.includes('仅记录'), 'report 模式要讲清不拦截');
  assert.ok(!h.includes('门禁模式'), 'report 模式不应自称门禁');
});

t('gate 模式标"门禁模式"', () => {
  assert.ok(qualityCard(rep({mode: 'gate'})).includes('门禁模式'));
});

t('WARN / FAIL 结论挂角标', () => {
  assert.ok(qualityCard(rep({verdict: 'WARN'})).includes('qverdict warn'));
  assert.ok(qualityCard(rep({verdict: 'FAIL'})).includes('qverdict bad'));
  assert.ok(!qualityCard(rep({verdict: 'PASS'})).includes('qverdict'), 'PASS 不该有角标');
});

t('多产物时附加产物的问题按文件名归属', () => {
  const h = qualityCard(rep({
    artifacts: [
      {file: '00-primary.mp4', score: 87, grade: 'A', issues: []},
      {file: '02-pose.mp4', score: 30, grade: 'D', issues: ['亮度均值 12 < 26：整体过暗']},
    ],
  }));
  assert.ok(h.includes('02-pose.mp4：亮度均值 12'), '附加产物 issue 要带文件名前缀');
  assert.ok(h.includes('qmore'), '多个产物应有 qchip 汇总');
  assert.ok(h.includes('qchip'), '应有 qchip');
});

t('文件名与 issue 都做 HTML 转义（产物名可能含 <）', () => {
  const h = qualityCard(rep({
    artifacts: [{file: '<b>.mp4', score: 30, grade: 'D', issues: ['<script>x</script>']}],
  }));
  assert.ok(!h.includes('<script>x'), 'issue 必须转义');
  assert.ok(h.includes('&lt;script&gt;'), '应出现转义后的文本');
});

t('QGRADE 四个档位齐全，且都能说出人话', () => {
  for (const k of ['A', 'B', 'C', 'D']) {
    assert.ok(QGRADE[k], `缺 ${k} 档`);
    assert.ok(QGRADE[k].say && QGRADE[k].say.length > 4, `${k} 档要有结论文案`);
    assert.ok(QGRADE[k].tone, `${k} 档要有颜色档位`);
  }
});

/* ---------------------------------------------------------------- 接入点 */
t('结果页真的插了质量卡（与 costCard 并列）', () => {
  assert.ok(/costCard\(S\.detail\.cost, job\)\}\s*\$\{qualityCard\(S\.detail\.quality\)\}/.test(src),
    'renderResult 里应紧跟 costCard 渲染 qualityCard(S.detail.quality)');
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
