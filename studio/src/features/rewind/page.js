/**
 * R62：独立的「回滚重跑」导航页。
 *
 * 与任务详情弹窗共用 postRewind / postResume 和 lib/rewind.js 的纯函数，只是把
 * 「选起点 → dry-run 预览影响集 → 确认 → 自动 resume」做成一条可反复操作的流程，
 * 不用每次都先点开某个任务详情。未连接后端 API 时只显示空态，不伪造动作。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';
import { emptyState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';
import { currentRunId } from '../../lib/guidance.js';
import {
  explainRewindError,
  needsResume,
  rewindImpactText,
  rewindTaskOptions,
} from '../../lib/rewind.js';
import { postResume, postRewind } from './index.js';

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted']);

function renderRewindPage(container) {
  const base = S.settings && S.settings.apiBaseUrl;
  const rid = currentRunId(S.dash && S.dash.run, S.watchDir);
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '回滚重跑'),
    h('span', { class: 'gate-full-id' }, rid || '未选择 run')));
  if (!S.realMode || !base || !rid) {
    container.appendChild(emptyState(
      '需要连接后端 API',
      '回滚通过 REST 控制面执行：请在「设置 → 连接」配置地址与具备 runs:write 的 Key。'));
    return;
  }
  const tasks = (S.dash && S.dash.tasks) || [];
  if (!tasks.length) {
    container.appendChild(emptyState('暂无任务', '当前运行没有可回滚的任务板。'));
    return;
  }
  const status = String((S.dash.run && S.dash.run.status) || '').toLowerCase();
  const options = rewindTaskOptions(tasks);
  const select = h('select', { class: 'rewind-select', 'aria-label': '回滚起点任务' });
  for (const opt of options) {
    select.appendChild(h('option', { value: opt.value }, opt.label));
  }
  const instruction = h('textarea', {
    class: 'rewind-input', rows: 2,
    placeholder: '可选：给这次重做补一句新要求（写进任务描述与验收标准）',
    'aria-label': '重做说明',
  });
  const resultBox = h('div', { class: 'rewind-result' });
  const token = S.navToken;
  let preview = null;

  const confirmButton = h('button', {
    class: 'btn btn-danger btn-sm', type: 'button', disabled: true,
    onclick: async () => {
      if (!preview) return;
      confirmButton.disabled = true;
      try {
        const result = await postRewind(rid, select.value, instruction.value, false);
        if (needsResume(result)) {
          try {
            await postResume(rid);
            toast('success', '已回滚并重跑', rewindImpactText(result));
          } catch (err) {
            toast('info', '已回滚，请手动恢复',
              `resume 未成功：${explainRewindError(err)}`);
          }
        } else {
          toast('success', '回滚完成', rewindImpactText(result));
        }
        preview = null;
        if (token === S.navToken && resultBox.isConnected) {
          resultBox.replaceChildren(h('div', { class: 'rewind-impact' },
            rewindImpactText(result)));
        }
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
        preview = await postRewind(rid, select.value, instruction.value, true);
        if (token !== S.navToken || !resultBox.isConnected) return;
        resultBox.replaceChildren(
          h('div', { class: 'rewind-impact' }, rewindImpactText(preview)));
        confirmButton.disabled = preview.affected.length === 0;
      } catch (err) {
        if (token === S.navToken && resultBox.isConnected) {
          resultBox.replaceChildren(
            h('div', { class: 'rewind-error' }, explainRewindError(err)));
        }
        toast('error', '影响集预览失败', explainRewindError(err));
      }
    },
  }, '预览影响集');

  const note = status && !TERMINAL.has(status)
    ? '当前 run 仍在运行中，后端会返回 409；请先取消（或等它结束）再回滚。'
    : '旧产物会先快照到 history/rewind-NNN/，再从 artifacts 与清单里移除。';
  container.appendChild(h('div', { class: 'rewind-page' },
    h('div', { class: 'rewind-page-note' }, `当前状态：${status || '未知'}。${note}`),
    h('div', { class: 'rewind-form' },
      h('label', { class: 'rewind-label' }, '起点任务'), select,
      h('label', { class: 'rewind-label' }, '重做说明'), instruction),
    h('div', { class: 'modal-actions' }, previewButton, confirmButton),
    resultBox,
    h('div', { class: 'rewind-page-help' },
      '影响集 = 起点任务 + 它的全部下游 + T-FINAL；上游已成功的任务与产物原样保留。'
      + '回滚后 run 置回 interrupted，本页会自动调用 resume 从该点重跑。')));
}

export { renderRewindPage };
