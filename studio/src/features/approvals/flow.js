/**
 * 审批裁决 + 自动续跑（门禁页与对话页内联卡共用，避免两处逻辑漂移）。
 *
 * 流程：POST /runs/{id}/approvals（裁决）→ 批准 / 接受时再 POST /runs/{id}/resume。
 * 续跑失败（409 = 别的进程正在 resume）不假装成功：把原因交给调用方写进状态槽，
 * 由调用方决定要不要给「重试恢复」按钮。
 */
import { apiFetch } from '../../lib/api.js';
import { decisionLabel, shouldAutoResume } from '../../lib/approval.js';
import { explainRewindError } from '../../lib/rewind.js';
import { toast } from '../../ui/toast.js';

import { postResume } from '../rewind/index.js';

async function loadApprovals(runId) {
  const payload = await apiFetch(`/runs/${encodeURIComponent(runId)}/approvals`);
  return (payload && payload.approvals) || [];
}

async function retryResume(runId) {
  await postResume(runId);
}

/**
 * 裁决一张审批单。`onState(text, tone)` 的 tone ∈ busy / ok / warn，
 * 调用方自己决定怎么呈现（门禁页是一行文字，内联卡是卡片内状态条）。
 */
async function decideApproval({ runId, item, action, onState }) {
  const state = (text, tone) => {
    if (typeof onState === 'function') onState(text, tone);
  };
  try {
    await apiFetch(`/runs/${encodeURIComponent(runId)}/approvals`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: item.id,
        decision: { action },
        decided_by: 'studio',
      }),
    });
    toast('success', '已裁决', `${item.id} → ${decisionLabel(action)}`);
  } catch (err) {
    toast('error', '裁决失败', String(err.detail || err.message || err));
    return { ok: false, resumed: false };
  }
  if (!shouldAutoResume(action)) {
    state(`已裁决（${decisionLabel(action)}），run 将按该决定收尾`, 'ok');
    return { ok: true, resumed: false };
  }
  state('已裁决，正在恢复运行…', 'busy');
  try {
    await postResume(runId);
    state('已恢复运行，从卡点继续', 'ok');
    toast('success', '已恢复', 'run 已从审批卡点继续执行');
    return { ok: true, resumed: true };
  } catch (err) {
    const detail = explainRewindError(err);
    state(`已裁决，待恢复：${detail}`, 'warn');
    return { ok: true, resumed: false, error: detail };
  }
}

export { decideApproval, loadApprovals, retryResume };
