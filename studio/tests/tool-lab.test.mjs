import { test } from 'node:test';
import assert from 'node:assert';

import {
  argsTemplate,
  formatDuration,
  kindLabel,
  parseArgs,
  resultSummary,
  statusChips,
} from '../src/lib/tool-lab.js';

const tool = (schema, over = {}) => ({
  name: 'read_file', kind: 'file', description: '读文件',
  input_schema: schema, ...over,
});

test('argsTemplate：只预填必填字段，可选字段留给用户加', () => {
  const t = argsTemplate(tool({
    type: 'object',
    properties: {
      path: { type: 'string' },
      max_bytes: { type: 'integer' },
      follow_symlinks: { type: 'boolean' },
    },
    required: ['path'],
  }));
  assert.deepEqual(t, { path: '' });
});

test('argsTemplate：无 schema / 无必填时给空对象', () => {
  assert.deepEqual(argsTemplate({}), {});
  assert.deepEqual(argsTemplate(tool({ properties: { a: { type: 'string' } } })), {});
});

test('parseArgs：空文本视为无入参，数组/标量被拒并给出可读错误', () => {
  assert.deepEqual(parseArgs('   '), { ok: true, args: {} });
  assert.deepEqual(parseArgs('{"path":"workspace/a.txt"}'),
    { ok: true, args: { path: 'workspace/a.txt' } });
  const arr = parseArgs('[1,2]');
  assert.equal(arr.ok, false);
  assert.match(arr.error, /JSON 对象/);
  const bad = parseArgs('{oops}');
  assert.equal(bad.ok, false);
  assert.match(bad.error, /JSON 解析失败/);
});

test('formatDuration：毫秒 / 秒两档，非法值给破折号', () => {
  assert.equal(formatDuration(12.34), '12.3 ms');
  assert.equal(formatDuration(1500), '1.50 s');
  assert.equal(formatDuration(-1), '—');
  assert.equal(formatDuration('x'), '—');
});

test('kindLabel：三类内置类别译成中文，未知类别原样返回', () => {
  assert.equal(kindLabel('file'), '文件');
  assert.equal(kindLabel('network'), '网络');
  assert.equal(kindLabel('sandbox'), '沙箱');
  assert.equal(kindLabel('mcp'), 'mcp');
  assert.equal(kindLabel(''), '其他');
});

test('statusChips：关闭项必须说明「清单里为什么没有那个工具」', () => {
  const off = statusChips({ tools_enabled: false, code_exec_enabled: false, search_provider: '' });
  assert.match(off[0].label, /试跑不可用/);
  assert.match(off[1].label, /不列 run_python/);
  assert.match(off[2].label, /不列 web_search/);
  assert.equal(off[0].tone, 'warn');

  const on = statusChips({
    tools_enabled: true, code_exec_enabled: true, sandbox_backend: 'docker',
    search_provider: 'tavily', trial_dir: '_tool_trial',
  });
  assert.equal(on[0].tone, 'ok');
  assert.equal(on[1].label, '代码执行：docker');
  assert.equal(on[1].tone, 'ok');
  assert.equal(on[2].label, '搜索提供方：tavily');
  assert.equal(on[3].label, '沙箱目录：_tool_trial/');
});

test('statusChips：local 后端标 warn（有沙箱和没沙箱是两回事）', () => {
  const chips = statusChips({ tools_enabled: true, code_exec_enabled: true, sandbox_backend: 'local' });
  assert.equal(chips[1].tone, 'warn');
});

test('resultSummary：ok=false 是执行失败，与请求错误分开叙述', () => {
  const ok = resultSummary({ ok: true, duration_ms: 8.2, sandbox: '_tool_trial/x' });
  assert.equal(ok.tone, 'ok');
  assert.match(ok.title, /试跑通过 · 8.2 ms/);
  assert.equal(ok.detail, '沙箱：_tool_trial/x');

  const fail = resultSummary({ ok: false, duration_ms: 30, error: 'TimeoutError：超时' });
  assert.equal(fail.tone, 'err');
  assert.match(fail.title, /试跑失败/);
  assert.equal(fail.detail, 'TimeoutError：超时');

  const silent = resultSummary({ ok: false });
  assert.match(silent.detail, /未带错误信息/);

  assert.equal(resultSummary(null).tone, 'mute');
});
