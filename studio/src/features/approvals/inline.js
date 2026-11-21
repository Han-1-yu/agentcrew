/**
 * 对话页内联审批卡（§5）：系统消息「等待人工审批」下面直接给裁决按钮。
 *
 * 数据走 REST `GET/POST /runs/{id}/approvals`（与门禁页同一个端点，不需要新 IPC）；
 * 裁决与自动续跑复用 `features/approvals/flow.js`，两边不会各跑一套。未连后端时
 * 不伪造按钮，降级成「去门禁页处理」。
 */
import { h } from '../../lib/dom.js';
import {
  actionsFor, inlineApprovalMode, pendingOf, runIdFrom,
} from '../../lib/approval.js';
import { S } from '../../lib/store.js';
import { toast } from '../../ui/toast.js';
import { switchNav } from '../../app/router.js';

import { decideApproval, loadApprovals, retryResume } from './flow.js';

const CHECKPOINT_LABEL = { plan: '计划卡点', delivery: '交付卡点' };

function gotoGatesButton() {
  return h('button', {
    class: 'btn btn-sm', type: 'button', onclick: () => switchNav('gates'),
  }, '去门禁页处理');
}

/** 建一张内联审批卡；`runId` 缺省时从当前 S.dash 的 run 推。 */
function inlineApprovalCard({ runId, checkpoint } = {}) {
  const rid = runId || runIdFrom(S.dash && S.dash.run, S.watchDir);
  const base = S.settings && S.settings.apiBaseUrl;
  const stateSlot = h('div', { class: 'conv-approval-state' });
  const setState = (text, tone) => {
    stateSlot.textContent = text || '';
    if (tone) stateSlot.dataset.tone = tone; else delete stateSlot.dataset.tone;
  };

  const box = h('div', { class: 'conv-approval', 'data-approval-card': '1' },
    h('div', { class: 'conv-approval-head' },
      h('span', { class: 'conv-approval-title' }, '等待人工审批'),
      h('span', { class: 'conv-approval-meta mono' },
        CHECKPOINT_LABEL[checkpoint] || (checkpoint ? String(checkpoint) : '待裁决'))));

  const mode = inlineApprovalMode({
    realMode: S.realMode, apiBaseUrl: base, runId: rid,
  });
  if (mode === 'offline') {
    // 没连 API 就没有裁决能力：明说原因，只给跳转（门禁页那边有完整说明）。
    box.appendChild(h('div', { class: 'conv-approval-note' },
      '未连接后端 API，这里不能直接裁决。到门禁页可以看审批单说明并处理。'));
    box.appendChild(h('div', { class: 'conv-approval-actions' }, gotoGatesButton()));
    return box;
  }

  const list = h('div', { class: 'conv-approval-list' });
  box.appendChild(list);
  box.appendChild(stateSlot);
  loadRows();

  async function loadRows() {
    let items = [];
    try {
      items = await loadApprovals(rid);
    } catch (err) {
      setState(`审批单读取失败：${String((err && (err.detail || err.message)) || err)}`, 'warn');
      box.appendChild(h('div', { class: 'conv-approval-actions' }, gotoGatesButton()));
      return;
    }
    const pending = pendingOf(items);
    if (!pending.length) {
      setState('没有待裁决的审批单（可能已在别处处理）', '');
      list.appendChild(h('div', { class: 'conv-approval-actions' }, gotoGatesButton()));
      return;
    }
    for (const item of pending) list.appendChild(approvalRow(item));
  }

  function approvalRow(item) {
    const actions = h('div', { class: 'conv-approval-actions' });
    const row = h('div', { class: 'conv-approval-row' },
      h('span', { class: 'conv-approval-id mono' }, item.id), actions);

    for (const [action, label] of actionsFor(item.checkpoint)) {
      actions.appendChild(h('button', {
        class: `btn btn-sm ${action === 'cancel' || action === 'abort' ? 'btn-danger' : 'btn-primary'}`,
        type: 'button',
        'data-decision': action,
        onclick: () => runDecision(item, action, row, actions),
      }, label));
    }
    return row;
  }

  async function runDecision(item, action, row, actions) {
    actions.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    const result = await decideApproval({ runId: rid, item, action, onState: setState });
    if (result.resumed) {
      row.classList.add('is-done');
      toast('success', '已恢复', `${item.id} 已批准并继续执行`);
      return;
    }
    if (result.ok) {
      // 裁决成功但没续跑（取消 / 放弃）或续跑失败：给重试入口，别让人卡在这里。
      row.classList.add('is-decided');
      if (result.error) {
        const retry = h('button', {
          class: 'btn btn-sm', type: 'button', onclick: async () => {
            retry.disabled = true;
            setState('正在恢复运行…', 'busy');
            try {
              await retryResume(rid);
              setState('已恢复运行，从卡点继续', 'ok');
              retry.remove();
            } catch (err) {
              setState(`仍未能恢复：${String((err && (err.detail || err.message)) || err)}`, 'warn');
              retry.disabled = false;
            }
          },
        }, '重试恢复');
        actions.appendChild(retry);
      }
      return;
    }
    actions.querySelectorAll('button').forEach((b) => { b.disabled = false; });
  }
}

export { inlineApprovalCard };
