/**
 * C10 切片二：run 中途指引的控制面 UI（门禁页顶部区块）。
 *
 * 全部走 REST：GET /runs/{id}/guidance、POST 同路径、DELETE .../guidance/{item_id}、
 * POST .../guidance/compact。和审批单一样，未连接后端 API 时只显示空态，
 * 不伪造动作；异步回来后比对 S.navToken，切页后不往已卸载的 DOM 写内容。
 */
import { h } from '../../lib/dom.js';
import { apiFetch } from '../../lib/api.js';
import { S } from '../../lib/store.js';
import { emptyState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';
import { confirmAction } from '../../ui/modal.js';
import {
  buildGuidanceBody,
  canWithdraw,
  currentRunId,
  explainGuidanceError,
  guidanceMeta,
  guidanceTaskOptions,
  normalizeGuidance,
} from '../../lib/guidance.js';

const EXPIRY_OPTIONS = [
  { value: '0', label: '不过期' },
  { value: '5', label: '5 分钟后过期' },
  { value: '30', label: '30 分钟后过期' },
  { value: '120', label: '2 小时后过期' },
];

function selectFrom(options, className, label) {
  const select = h('select', { class: className, 'aria-label': label });
  for (const opt of options) {
    select.appendChild(h('option', { value: opt.value }, opt.label));
  }
  return select;
}

async function load(list, rid) {
  const token = S.navToken;
  list.innerHTML = '';
  let normalized;
  try {
    const payload = await apiFetch(`/runs/${encodeURIComponent(rid)}/guidance`);
    if (token !== S.navToken || !list.isConnected) return;
    normalized = normalizeGuidance(payload);
  } catch (err) {
    if (token === S.navToken && list.isConnected) {
      list.appendChild(emptyState('指引不可读', explainGuidanceError(err)));
    }
    return;
  }
  if (!normalized.items.length) {
    list.appendChild(emptyState(
      '暂无中途指引', '运行中补一句约束，引擎会在下一次任务派发前吸收。'));
    return;
  }
  for (const item of normalized.items) {
    const meta = guidanceMeta(item);
    const head = h('div', { class: 'guidance-item-head' },
      h('span', { class: 'guidance-id' }, item.id),
      h('span', { class: `guidance-status guidance-${item.status}` }, item.label));
    if (canWithdraw(item)) {
      head.appendChild(h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => confirmAction(
          '撤回指引', `撤回 ${item.id} 后引擎将不再吸收它（历史仍保留）。`,
          () => withdraw(list, rid, item.id), '撤回'),
      }, '撤回'));
    }
    list.appendChild(h('div', { class: 'guidance-item' },
      head,
      h('div', { class: 'guidance-text' }, item.text),
      meta ? h('div', { class: 'guidance-meta' }, meta) : null));
  }
}

async function withdraw(list, rid, itemId) {
  try {
    await apiFetch(
      `/runs/${encodeURIComponent(rid)}/guidance/${encodeURIComponent(itemId)}`,
      { method: 'DELETE' });
    toast('success', '指引已撤回', `${itemId} 不再被吸收`);
    await load(list, rid);
  } catch (err) {
    toast('error', '撤回失败', explainGuidanceError(err));
  }
}

async function submit(list, rid, textInput, targetSelect, expirySelect) {
  const body = buildGuidanceBody({
    text: textInput.value,
    target: targetSelect.value,
    expiresMinutes: expirySelect.value,
  });
  if (!body.text) {
    toast('error', '指引不能为空', '先写一句要补的约束');
    return;
  }
  try {
    await apiFetch(`/runs/${encodeURIComponent(rid)}/guidance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    textInput.value = '';
    toast('success', '指引已送达', '下一次任务派发前会被吸收（at-most-once）');
    await load(list, rid);
  } catch (err) {
    toast('error', '指引发送失败', explainGuidanceError(err));
  }
}

async function compact(list, rid) {
  try {
    const result = await apiFetch(
      `/runs/${encodeURIComponent(rid)}/guidance/compact`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
    toast('success', '指引历史已压缩',
      `${result.before_lines} 行 → ${result.after_lines} 行，保留 ${result.kept} 条`);
    await load(list, rid);
  } catch (err) {
    toast('error', '压缩失败', explainGuidanceError(err));
  }
}

/** 在门禁页顶部挂「运行中指引」区块；返回容器便于调用方持有。 */
function renderGuidanceSection(container) {
  const box = h('div', { class: 'guidance-section' });
  container.appendChild(box);
  const base = S.settings && S.settings.apiBaseUrl;
  const rid = currentRunId(S.dash && S.dash.run, S.watchDir);
  if (!S.realMode || !base || !rid) {
    box.appendChild(emptyState(
      '运行中指引',
      '连接后端 API 并选择 run 后，可在这里补约束、定向到任务、撤回或设过期。'));
    return box;
  }

  box.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '运行中指引'),
    h('span', { class: 'gate-full-id' }, rid)));
  const textInput = h('textarea', {
    class: 'guidance-input', rows: 2,
    placeholder: '例如：把登录页改成绿色；不要动旧接口',
    'aria-label': '指引内容',
  });
  const targetSelect = selectFrom(
    guidanceTaskOptions(S.dash && S.dash.tasks),
    'guidance-select', '定向到任务');
  const expirySelect = selectFrom(EXPIRY_OPTIONS, 'guidance-select', '过期时间');
  const list = h('div', { class: 'guidance-list' });
  const actions = h('div', { class: 'guidance-actions' },
    h('button', {
      class: 'btn btn-primary btn-sm', type: 'button',
      onclick: () => submit(list, rid, textInput, targetSelect, expirySelect),
    }, '发送指引'),
    h('button', {
      class: 'btn btn-sm', type: 'button', onclick: () => load(list, rid),
    }, '刷新'),
    h('button', {
      class: 'btn btn-sm', type: 'button', onclick: () => compact(list, rid),
    }, '压缩历史'));
  box.appendChild(h('div', { class: 'guidance-form' },
    textInput,
    h('div', { class: 'guidance-form-row' }, targetSelect, expirySelect, actions)));
  box.appendChild(h('div', { class: 'guidance-hint' },
    '需要后端 guidance_enabled=true 才会被吸收（默认关）；一条指引只喂一个任务。压缩历史仅对已结束的 run 可用。'));
  box.appendChild(list);
  load(list, rid).catch((err) => {
    if (list.isConnected) {
      list.innerHTML = '';
      list.appendChild(emptyState('指引读取失败', explainGuidanceError(err)));
    }
  });
  return box;
}

export { renderGuidanceSection };
