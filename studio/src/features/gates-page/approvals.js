/**
 * R4 人工审批单（durable human-in-the-loop）。
 *
 * 后端把审批请求持久化在 <run_dir>/approvals.jsonl，run 以 awaiting_approval
 * 挂起；这里通过 REST 在**另一个进程/界面**里读取并裁决，引擎侧随后 resume
 * 从原卡点继续。演示模式 / 未配置 API 时只显示空态，不伪造审批动作。
 *
 * 端点：GET /runs/{id}/approvals、POST /runs/{id}/approvals
 * 复用既有 .btn / .gate-full-card / .page-head 样式，不新增样式分片。
 */
import { h } from '../../lib/dom.js';
import { apiFetch } from '../../lib/api.js';
import { S } from '../../lib/store.js';
import { emptyState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

/** run 目录名（去掉盘符与上级路径），与后端 _validated_run_id 同口径。 */
function currentRunId() {
  const run = (S.dash && S.dash.run) || {};
  const raw = run.run_id || run.dir || S.watchDir || '';
  return String(raw).replace(/\\/g, '/').split('/').filter(Boolean).pop() || '';
}

const ACTIONS = {
  plan: [['approve', '批准执行'], ['cancel', '取消运行']],
  delivery: [['accept', '接受交付'], ['abort', '放弃交付']],
};
const STATUS_LABEL = {
  pending: '等待裁决', decided: '已裁决，待 resume', consumed: '已消费',
};

async function decide(rid, item, action) {
  try {
    await apiFetch(`/runs/${encodeURIComponent(rid)}/approvals`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: item.id,
        decision: { action },
        decided_by: 'studio',
      }),
    });
    toast('success', '已裁决', `${item.id} → ${action}；用 resume 从卡点继续`);
  } catch (err) {
    toast('error', '裁决失败', String(err.detail || err.message || err));
  }
}

async function load(box, rid) {
  const token = S.navToken;
  box.innerHTML = '';
  box.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '人工审批'),
    h('span', { class: 'gate-full-id' }, rid)));
  let payload;
  try {
    payload = await apiFetch(`/runs/${encodeURIComponent(rid)}/approvals`);
  } catch (err) {
    if (token === S.navToken) box.appendChild(emptyState('审批单不可读', String(err.detail || err.message || err)));
    return;
  }
  // 等审批单回来时用户可能已经切页，别再往已卸载的容器写内容。
  if (token !== S.navToken) return;
  const items = (payload && payload.approvals) || [];
  if (!items.length) {
    box.appendChild(emptyState('暂无审批单', '本次运行没有需要人工裁决的节点。'));
    return;
  }
  const list = h('div', { class: 'gates-full-list' });
  box.appendChild(list);
  for (const item of items) {
    const row = h('div', { class: 'gate-full-card' },
      h('div', { class: 'gate-full-head' },
        h('span', { class: 'gate-full-id' }, item.id),
        h('span', { class: 'gate-full-name' }, item.checkpoint === 'plan' ? '计划卡点' : '交付卡点'),
        h('span', { class: 'gate-full-status' }, STATUS_LABEL[item.status] || item.status)),
      h('div', { class: 'gate-full-verdict' },
        item.decision && item.decision.action
          ? `裁决：${item.decision.action}${item.decided_by ? `（${item.decided_by}）` : ''}`
          : '尚未裁决'));
    if (item.status === 'pending') {
      const actions = h('div', { class: 'gate-full-actions' });
      for (const [action, label] of (ACTIONS[item.checkpoint] || [])) {
        actions.appendChild(h('button', {
          class: `btn btn-sm ${action === 'cancel' || action === 'abort' ? 'btn-danger' : 'btn-primary'}`,
          onclick: async () => {
            await decide(rid, item, action);
            if (box.isConnected) await load(box, rid);
          },
        }, label));
      }
      row.appendChild(actions);
    }
    list.appendChild(row);
  }
}

/** 在门禁页顶部挂一个审批单区块；返回容器便于调用方持有。 */
function renderApprovalsSection(container) {
  const box = h('div', { class: 'approvals-section' });
  container.appendChild(box);
  const base = S.settings && S.settings.apiBaseUrl;
  const rid = currentRunId();
  if (!S.realMode || !base || !rid) {
    box.appendChild(emptyState(
      '暂无待审批',
      '连接后端 API 并选择 run 后，可在此处理人工审批单。'));
    return box;
  }
  load(box, rid).catch(() => {});
  return box;
}

export { renderApprovalsSection };
