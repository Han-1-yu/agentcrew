/**
 * `lib/approval.js`：审批单的纯逻辑（门禁页与对话页内联卡共用）。
 * 重点是三件事别各写一份：动作表、状态文案、以及「内联卡该不该就地给按钮」。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  actionsFor, decisionLabel, inlineApprovalMode, pendingOf, runIdFrom,
  shouldAutoResume, statusLabel,
} from '../src/lib/approval.js';

test('actionsFor：计划卡点给批准/取消，交付卡点给接受/放弃，未知卡点为空', () => {
  assert.deepEqual(actionsFor('plan'), [['approve', '批准执行'], ['cancel', '取消运行']]);
  assert.deepEqual(actionsFor('delivery'), [['accept', '接受交付'], ['abort', '放弃交付']]);
  assert.deepEqual(actionsFor('unknown'), []);
  assert.deepEqual(actionsFor(undefined), []);
});

test('statusLabel 覆盖三个后端状态，未知值原样返回', () => {
  assert.equal(statusLabel('pending'), '等待裁决');
  assert.equal(statusLabel('decided'), '已裁决，待 resume');
  assert.equal(statusLabel('consumed'), '已消费');
  assert.equal(statusLabel('weird'), 'weird');
  assert.equal(statusLabel(undefined), '');
});

test('只有 approve / accept 需要自动 resume', () => {
  assert.equal(shouldAutoResume('approve'), true);
  assert.equal(shouldAutoResume('accept'), true);
  assert.equal(shouldAutoResume('cancel'), false);
  assert.equal(shouldAutoResume('abort'), false);
  assert.equal(decisionLabel('approve'), '批准执行');
  assert.equal(decisionLabel('nope'), 'nope');
});

test('pendingOf 只留待裁决的单（已消费的再点会被后端拒）', () => {
  const items = [
    { id: 'a', status: 'pending' },
    { id: 'b', status: 'consumed' },
    { id: 'c', status: 'decided' },
    null,
  ];
  assert.deepEqual(pendingOf(items).map((i) => i.id), ['a']);
  assert.deepEqual(pendingOf(null), []);
});

test('runIdFrom 兼容 Windows 路径 / run_id / watchDir 兜底', () => {
  assert.equal(runIdFrom({ dir: 'E:\\agentcrew\\output\\20261121-213845-8f0c' }), '20261121-213845-8f0c');
  assert.equal(runIdFrom({ run_id: '20261121-213845-8f0c' }), '20261121-213845-8f0c');
  assert.equal(runIdFrom(null, '/tmp/o/20261101-010101-abcd'), '20261101-010101-abcd');
  assert.equal(runIdFrom({}, ''), '');
  // run_id 优先于 dir（dir 可能是更长的监视路径）
  assert.equal(runIdFrom({ run_id: 'a', dir: 'x/y/a' }), 'a');
});

test('inlineApprovalMode：没连 API 就 offline，连上但无 pending 单是 empty', () => {
  assert.equal(inlineApprovalMode({ realMode: false, apiBaseUrl: 'http://x', runId: 'r', approvals: [] }), 'offline');
  assert.equal(inlineApprovalMode({ realMode: true, apiBaseUrl: '', runId: 'r' }), 'offline');
  assert.equal(inlineApprovalMode({ realMode: true, apiBaseUrl: 'http://x', runId: '' }), 'offline');
  assert.equal(inlineApprovalMode({ realMode: true, apiBaseUrl: 'http://x', runId: 'r', approvals: [] }), 'empty');
  assert.equal(inlineApprovalMode({
    realMode: true, apiBaseUrl: 'http://x', runId: 'r',
    approvals: [{ id: 'a', status: 'consumed' }],
  }), 'empty');
  assert.equal(inlineApprovalMode({
    realMode: true, apiBaseUrl: 'http://x', runId: 'r',
    approvals: [{ id: 'a', status: 'pending' }],
  }), 'ready');
});
