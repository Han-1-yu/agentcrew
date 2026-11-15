/**
 * R62：Studio 的「从此恢复 / 回滚重跑」入口。
 *
 * 后端 rewind 是两步：POST /runs/{id}/rewind（可 dry_run 预览影响集）→
 * POST /runs/{id}/resume 才真正重跑。这里先强制预览再允许确认，避免用户
 * 在不知道会删掉哪些产物的情况下点下回滚。未连接 API 时只给提示，不伪造动作。
 */
import { h } from '../../lib/dom.js';
import { apiFetch } from '../../lib/api.js';
import { S } from '../../lib/store.js';
import { toast } from '../../ui/toast.js';
import { closeModal, showModal } from '../../ui/modal.js';
import {
  buildRewindBody,
  explainRewindError,
  needsResume,
  normalizeRewindResult,
  rewindImpactText,
  rewindTaskOptions,
} from '../../lib/rewind.js';
import { currentRunId } from '../../lib/guidance.js';

async function postRewind(rid, task, instruction, dryRun) {
  const payload = await apiFetch(`/runs/${encodeURIComponent(rid)}/rewind`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildRewindBody({ task, instruction, dryRun })),
  });
  return normalizeRewindResult(payload);
}

/** 从任务详情 / 运行面板打开回滚弹窗；taskId 为空时取任务板第一项。 */
function openRewindDialog(taskId) {
  const base = S.settings && S.settings.apiBaseUrl;
  const rid = currentRunId(S.dash && S.dash.run, S.watchDir);
  if (!S.realMode || !base || !rid) {
    toast('info', '需要连接后端 API',
      '回滚通过 REST 控制面执行：请在「设置 → 连接」配置地址与具备 runs:write 的 Key。');
    return;
  }
  const tasks = (S.dash && S.dash.tasks) || [];
  const options = rewindTaskOptions(tasks, taskId);
  const preferred = String(taskId || (options[0] && options[0].value) || '');
  const taskSelect = h('select', { class: 'rewind-select', 'aria-label': '回滚起点任务' });
  for (const opt of options) {
    taskSelect.appendChild(h('option', { value: opt.value }, opt.label));
  }
  taskSelect.value = preferred;
  const instruction = h('textarea', {
    class: 'rewind-input', rows: 2,
    placeholder: '可选：给这次重做补一句新要求（会写进任务描述与验收标准）',
    'aria-label': '重做说明',
  });
  const resultBox = h('div', { class: 'rewind-result' });
  let preview = null;

  const confirmButton = h('button', {
    class: 'btn btn-danger btn-sm', type: 'button', disabled: true,
    onclick: async () => {
      if (!preview) return;
      confirmButton.disabled = true;
      try {
        const result = await postRewind(
          rid, taskSelect.value, instruction.value, false);
        if (needsResume(result)) {
          try {
            await apiFetch(`/runs/${encodeURIComponent(rid)}/resume`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: '{}',
            });
            toast('success', '已回滚并重跑', rewindImpactText(result));
          } catch (err) {
            toast('info', '已回滚，请手动恢复',
              `resume 未成功：${explainRewindError(err)}`);
          }
        } else {
          toast('success', '回滚完成', rewindImpactText(result));
        }
        closeModal();
      } catch (err) {
        confirmButton.disabled = false;
        toast('error', '回滚失败', explainRewindError(err));
      }
    },
  }, '回滚并重跑');

  const previewButton = h('button', {
    class: 'btn btn-sm', type: 'button',
    onclick: async () => {
      preview = null;
      confirmButton.disabled = true;
      resultBox.replaceChildren(h('div', { class: 'rewind-hint' }, '正在计算影响集…'));
      try {
        preview = await postRewind(
          rid, taskSelect.value, instruction.value, true);
        resultBox.replaceChildren(
          h('div', { class: 'rewind-impact' }, rewindImpactText(preview)));
        confirmButton.disabled = preview.affected.length === 0;
      } catch (err) {
        resultBox.replaceChildren(
          h('div', { class: 'rewind-error' }, explainRewindError(err)));
        toast('error', '影响集预览失败', explainRewindError(err));
      }
    },
  }, '预览影响集');

  const box = h('div', { class: 'rewind-dialog' },
    h('h3', { class: 'modal-title' }, '回滚到指定任务并重跑'),
    h('div', { class: 'confirm-text' },
      `run ${rid}：从所选任务起，其下游与 T-FINAL 都会被重置，旧产物先快照再删除。`
      + '运行中的 run 会返回 409，请先取消。'),
    h('div', { class: 'rewind-form' },
      h('label', { class: 'rewind-label' }, '起点任务'), taskSelect,
      h('label', { class: 'rewind-label' }, '重做说明'), instruction),
    resultBox,
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '取消'),
      previewButton, confirmButton));
  showModal(box);
}

export { openRewindDialog };
