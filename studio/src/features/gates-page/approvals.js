/**
 * R4 人工审批单（durable human-in-the-loop）+ I1 自动恢复 + I2 裁决上下文。
 *
 * 后端把审批请求持久化在 <run_dir>/approvals.jsonl，run 以 awaiting_approval
 * 挂起；这里通过 REST 在**另一个进程/界面**里读取并裁决。批准 / 接受后自动
 * 调 POST /runs/{id}/resume（I1）——409（他进程占用）不假装成功，显示状态与
 * 手动重试按钮。审批卡带上下文（I2）：计划卡点给任务/角色数与「查看计划」，
 * 交付卡点给产物摘要与「查看产物」，让人看着内容裁决。
 *
 * 端点：GET/POST /runs/{id}/approvals、POST /runs/{id}/resume
 */
import { h } from '../../lib/dom.js';
import { apiFetch } from '../../lib/api.js';
import { explainRewindError } from '../../lib/rewind.js';
import { S } from '../../lib/store.js';
import { emptyState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';
import { switchNav } from '../../app/router.js';
import { postResume } from '../rewind/index.js';

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
const AUTO_RESUME_ACTIONS = new Set(['approve', 'accept']);

/* ---- I2：裁决上下文（数据全部来自已在内存的 S.dash，不发额外请求） ---- */

function approvalContext(item) {
  const dash = S.dash || {};
  const tasks = dash.tasks || [];
  if (item.checkpoint === 'plan') {
    if (!tasks.length) return null;
    const roles = new Set(tasks.map((t) => t.role));
    return {
      text: `本次计划共 ${tasks.length} 个任务，涉及 ${roles.size} 个角色`,
      action: { label: '查看计划', onclick: () => switchNav('tasks') },
    };
  }
  if (item.checkpoint === 'delivery') {
    const arts = (dash.events || []).filter((e) => e.event === 'artifact_written' && e.data && e.data.path);
    const finalTask = tasks.find((t) => t.id === 'T-FINAL');
    const bits = [`交付卡点：T-FINAL ${finalTask ? finalTask.status : '—'}，本次已产出 ${arts.length} 个产物`];
    if (arts.length) bits.push(`最近：${arts.slice(-2).map((e) => String(e.data.path).split('/').pop()).join('、')}`);
    return {
      text: bits.join(' · '),
      action: { label: '查看产物', onclick: () => switchNav('artifacts') },
    };
  }
  return null;
}

async function decide(rid, item, action, resumeSlot, retryBtn) {
  const showResume = (text, cls) => {
    if (resumeSlot) { resumeSlot.textContent = text; resumeSlot.className = cls; }
  };
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
    toast('success', '已裁决', `${item.id} → ${action}`);
    if (!AUTO_RESUME_ACTIONS.has(action)) {
      showResume(`已裁决（${action}），run 将按该决定收尾`, 'gate-full-verdict');
      return;
    }
    // I1：批准 / 接受后自动恢复。409 = run 正被别的进程 resume，如实呈现。
    showResume('已裁决，正在恢复运行…', 'gate-full-verdict');
    try {
      await postResume(rid);
      showResume('已恢复运行，从卡点继续', 'gate-full-verdict gate-resume-ok');
      toast('success', '已恢复', 'run 已从审批卡点继续执行');
    } catch (err) {
      showResume(`已裁决，待恢复：${explainRewindError(err)}`, 'gate-full-verdict gate-resume-warn');
      if (retryBtn) retryBtn.classList.remove('hidden');
    }
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
    const ctx = approvalContext(item);
    const resumeSlot = h('div', { class: 'gate-full-verdict' }, '');
    const retryBtn = h('button', {
      class: 'btn btn-sm hidden', type: 'button', title: '再次尝试恢复运行',
      onclick: async () => {
        retryBtn.disabled = true;
        showResumeIn(resumeSlot, '正在恢复运行…');
        try {
          await postResume(rid);
          showResumeIn(resumeSlot, '已恢复运行，从卡点继续', 'gate-full-verdict gate-resume-ok');
          retryBtn.classList.add('hidden');
        } catch (err) {
          showResumeIn(resumeSlot, `仍未能恢复：${explainRewindError(err)}`, 'gate-full-verdict gate-resume-warn');
        }
        retryBtn.disabled = false;
      },
    }, '重试恢复');

    const row = h('div', { class: 'gate-full-card' },
      h('div', { class: 'gate-full-head' },
        h('span', { class: 'gate-full-id' }, item.id),
        h('span', { class: 'gate-full-name' }, item.checkpoint === 'plan' ? '计划卡点' : '交付卡点'),
        h('span', { class: 'gate-full-status' }, STATUS_LABEL[item.status] || item.status)),
      ctx ? h('div', { class: 'gate-full-verdict' },
        ctx.text + ' ',
        h('button', { class: 'btn btn-sm', type: 'button', onclick: ctx.action.onclick }, ctx.action.label))
        : h('div', { class: 'gate-full-verdict' },
          item.decision && item.decision.action
            ? `裁决：${item.decision.action}${item.decided_by ? `（${item.decided_by}）` : ''}`
            : '尚未裁决'),
      resumeSlot);

    if (item.status === 'pending') {
      const actions = h('div', { class: 'gate-full-actions' });
      for (const [action, label] of (ACTIONS[item.checkpoint] || [])) {
        actions.appendChild(h('button', {
          class: `btn btn-sm ${action === 'cancel' || action === 'abort' ? 'btn-danger' : 'btn-primary'}`,
          onclick: async () => {
            // 不整卡重载：resume 的进行时/结果写进 resumeSlot，让用户看到闭环；
            // 列表状态下次进入本页自然刷新。
            await decide(rid, item, action, resumeSlot, retryBtn);
            actions.querySelectorAll('button').forEach((b) => { b.disabled = true; });
            if (retryBtn && !retryBtn.classList.contains('hidden')) actions.appendChild(retryBtn);
          },
        }, label));
      }
      row.appendChild(actions);
    }
    list.appendChild(row);
  }
}

function showResumeIn(slot, text, cls) {
  if (!slot) return;
  slot.textContent = text;
  slot.className = cls || 'gate-full-verdict';
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
  // C-10：不能静默吞掉——读取失败时界面会一直停在「正在读取…」，
  // 用户既不知道有没有审批单，也不知道是网络/后端问题。
  load(box, rid).catch((e) => {
    box.innerHTML = '';
    box.appendChild(emptyState(
      '审批单读取失败',
      String((e && e.message) || e || '未知错误')));
  });
  return box;
}

export { renderApprovalsSection };
