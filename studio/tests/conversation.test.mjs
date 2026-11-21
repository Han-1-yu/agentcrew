import { test } from 'node:test';
import assert from 'node:assert';

import {
  CONV_FILTERS,
  agentMessageText,
  buildTranscript,
  filterCounts,
  filterMessages,
  messageInFilter,
  summarizeToolArgs,
  transcriptFilename,
} from '../src/lib/conversation.js';

const msgs = [
  { type: 'user', order: 0, goal: '开发 Todo 应用', ts: 't0' },
  { type: 'system', order: 1, kind: 'plan', text: '已拆解为 8 个任务', ts: 't1' },
  {
    type: 'agent', order: 2, taskId: 'T-001', role: 'product_manager', ts: 't2',
    items: [
      { kind: 'note', text: '先写 PRD，再写验收标准' },
      { kind: 'tool', tool: 'write_file', args: { path: 'docs/PRD.md' }, ok: true, dur: 12 },
      { kind: 'gate', layer: 'static', verdict: 'approve' },
      { kind: 'artifact', path: 'docs/PRD.md', size: 2730 },
      { kind: 'finish', dur: 1200 },
    ],
  },
  {
    type: 'agent', order: 3, taskId: 'T-002', role: 'backend_dev', ts: 't3',
    items: [{ kind: 'gate', layer: 'llm', verdict: 'reject', issues: ['缺少错误处理'] }],
  },
];
const taskIndex = { 'T-001': { title: '产品需求' }, 'T-002': { title: '后端实现' } };

test('类型筛选：user/system/agent 与条目类筛选', () => {
  assert.equal(filterMessages(msgs, { filter: 'user' }).length, 1);
  assert.equal(filterMessages(msgs, { filter: 'system' }).length, 1);
  assert.equal(filterMessages(msgs, { filter: 'agent' }).length, 2);
  assert.equal(filterMessages(msgs, { filter: 'tool' }).length, 1);
  assert.equal(filterMessages(msgs, { filter: 'artifact' }).length, 1);
  assert.equal(filterMessages(msgs, { filter: 'gate' }).length, 2);
  assert.equal(filterMessages(msgs, { filter: 'all' }).length, 4);
});

test('筛选 key 都有对应 chip，未知 key 退回全部', () => {
  assert.deepEqual(CONV_FILTERS.map((f) => f.key), ['all', 'user', 'agent', 'system', 'tool', 'artifact', 'gate']);
  assert.equal(messageInFilter(msgs[0], 'nope'), true);
});

test('搜索覆盖目标、正文、角色、任务标题、工具入参、产物路径与门禁意见', () => {
  const hit = (q) => filterMessages(msgs, { query: q }, taskIndex).map((m) => m.order);
  assert.deepEqual(hit('Todo'), [0]);
  assert.deepEqual(hit('8 个任务'), [1]);
  assert.deepEqual(hit('PRD'), [2]);
  assert.deepEqual(hit('产品需求'), [2]);
  assert.deepEqual(hit('write_file'), [2]);
  assert.deepEqual(hit('docs/PRD.md'), [2]);
  assert.deepEqual(hit('缺少错误处理'), [3]);
  assert.deepEqual(hit('后端'), [3]);
  assert.equal(filterMessages(msgs, { query: '   ' }).length, 4);
});

test('搜索 + 类型筛选是「与」关系', () => {
  const out = filterMessages(msgs, { query: 'PRD', filter: 'gate' }, taskIndex);
  assert.deepEqual(out.map((m) => m.taskId), ['T-001']);
  assert.equal(filterMessages(msgs, { query: 'PRD', filter: 'user' }, taskIndex).length, 0);
});

test('filterCounts 重叠计数（门禁/工具各算各的）', () => {
  const c = filterCounts(msgs);
  assert.equal(c.all, 4);
  assert.equal(c.tool, 1);
  assert.equal(c.gate, 2);
  assert.equal(c.agent, 2);
});

test('工具摘要优先取 path/command，字符串入参单行截断', () => {
  assert.equal(summarizeToolArgs({ path: 'docs/PRD.md', other: 1 }), 'docs/PRD.md');
  assert.equal(summarizeToolArgs({ command: 'npm test\n --watch', cmd: 'x' }), 'npm test --watch');
  assert.equal(summarizeToolArgs({ a: 1, b: 2 }), '{"a":1,"b":2}');
  assert.equal(summarizeToolArgs(''), '');
  assert.equal(summarizeToolArgs(null), '');
  assert.ok(summarizeToolArgs({ path: 'x'.repeat(200) }, 20).length <= 20);
});

test('Agent 消息纯文本包含角色、备注、工具/门禁/产物摘要', () => {
  const text = agentMessageText(msgs[2], taskIndex['T-001']);
  assert.match(text, /产品经理 · T-001 · 产品需求/);
  assert.match(text, /先写 PRD/);
  assert.match(text, /工具 write_file（成功，12ms）：docs\/PRD\.md/);
  assert.match(text, /门禁 静态校验：通过/);
  assert.match(text, /产物 docs\/PRD\.md/);
  assert.doesNotMatch(text, /\{"path"/); // 不把原始 JSON 塞给用户
});

test('导出 Markdown：目标、运行信息、逐条消息、固定时间戳', () => {
  const md = buildTranscript(msgs, taskIndex, {
    run_id: 'r-1', status: 'completed', model: 'openai:gpt-6.1-sol', project_goal: '开发 Todo 应用',
  }, Date.UTC(2026, 10, 20, 12, 0, 0));
  assert.match(md, /^# 开发 Todo 应用/);
  assert.match(md, /运行 `r-1` · 状态 completed · 模型 openai:gpt-6\.1-sol · 导出 2026-11-20 12:00:00/);
  assert.match(md, /## 用户目标\n\n开发 Todo 应用/);
  assert.match(md, /> 已拆解为 8 个任务/);
  assert.match(md, /## 产品经理 · T-001 产品需求/);
  assert.match(md, /- 门禁 LLM 评审：驳回/);
  assert.ok(!/\n{3,}/.test(md), '不应出现三连空行');
  assert.ok(md.endsWith('\n'));
});

test('导出文件名取 run_id 末段并清掉非法字符', () => {
  assert.equal(transcriptFilename({ run_id: 'abc-123' }), 'agentcrew-abc-123-conversation.md');
  assert.equal(transcriptFilename({ dir: 'E:\\output\\x y\\r 1' }), 'agentcrew-r-1-conversation.md');
  assert.equal(transcriptFilename(null), 'agentcrew-run-conversation.md');
});

test('空输入不抛错', () => {
  assert.deepEqual(filterMessages(null, null), []);
  assert.equal(buildTranscript([], {}, {}, 0).includes('运行'), true);
  assert.deepEqual(filterCounts(null), { all: 0, user: 0, agent: 0, system: 0, tool: 0, artifact: 0, gate: 0 });
});
