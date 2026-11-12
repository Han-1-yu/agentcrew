import { test } from 'node:test';
import assert from 'node:assert';
import { renderMarkdown } from '../src/lib/markdown.js';

test('表格渲染为 table 元素', () => {
  const out = renderMarkdown('| 用户 | 诉求 |\n| --- | --- |\n| 小明 | 记账 |');
  assert.match(out, /<table>/);
  assert.match(out, /<th>用户<\/th>/);
  assert.match(out, /<td>小明<\/td>/);
});

test('HTML 被转义，不产生可执行节点', () => {
  assert.equal(renderMarkdown('<script>alert(1)</script>'), '&lt;script&gt;alert(1)&lt;/script&gt;');
  assert.equal(renderMarkdown('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;');
});

test('危险链接保留原文、不生成 a 标签', () => {
  assert.equal(renderMarkdown('[点我](javascript:alert(1))'), '[点我](javascript:alert(1))');
  assert.equal(renderMarkdown('[点我](javascript:alert)'), '[点我](javascript:alert)');
  assert.doesNotMatch(renderMarkdown('[点我](data:text/html,x)'), /<a /);
});

test('安全链接生成带 rel 的 a 标签', () => {
  const out = renderMarkdown('[点我](https://a.com)');
  assert.match(out, /<a href="https:\/\/a.com"/);
  assert.match(out, /rel="noopener noreferrer"/);
});

test('标题 / 粗体 / 行内代码', () => {
  const out = renderMarkdown('# 标题\n\n**粗体** 和 `代码`');
  assert.match(out, /<h1>标题<\/h1>/);
  assert.match(out, /<strong>粗体<\/strong>/);
  assert.match(out, /<code>代码<\/code>/);
});

test('代码块内容转义', () => {
  const out = renderMarkdown('```js\nconst x = 1;\n```');
  assert.match(out, /<pre><code>const x = 1;<\/code><\/pre>/);
});

test('无序 / 有序列表', () => {
  assert.match(renderMarkdown('- a\n- b'), /<ul><li>a<\/li><li>b<\/li><\/ul>/);
  assert.match(renderMarkdown('1. x\n2. y'), /<ol><li>x<\/li><li>y<\/li><\/ol>/);
});

test('空 / null 输入返回空串', () => {
  assert.equal(renderMarkdown(''), '');
  assert.equal(renderMarkdown(null), '');
  assert.equal(renderMarkdown(undefined), '');
});
