/**
 * 单条日程卡片：状态胶囊 + 什么时候跑 + 启停 / 编辑 / 删除。
 *
 * 启停走 `schedule:setEnabled`（CLI `schedule enable|disable`），前端不碰
 * schedules.json —— tick 是另一个进程，两处直写会互相覆盖。
 */
import { h } from '../../lib/dom.js';
import { MODES } from '../../lib/mode.js';
import { stateOf, whenLabel } from '../../lib/schedule-draft.js';
import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

function scheduleCard(schedule, { onChanged, onEdit, triggers = [] }) {
  const s = schedule;
  const state = stateOf(s);
  const modeLabel = (MODES[s.mode] && MODES[s.mode].mode) || s.mode;
  // 触发历史来自各 run 的 ledger（`run_triggered source=schedule:<id>`），
  // 只扫最近 N 个 run：有记录就说次数与最近一次，没有就明说扫描范围。
  const historyLine = triggers.length
    ? `触发历史：${triggers.length} 次 · 最近 ${triggers[0].runDir}`
    : '触发历史：最近 30 个运行里没有记录';
  const toggle = h('button', {
    class: 'btn btn-sm schedule-toggle',
    'data-enabled': s.enabled ? '1' : '0',
    onclick: () => toggleEnabled(toggle),
  }, s.enabled ? '停用' : '启用');

  async function toggleEnabled(button) {
    const wanted = button.dataset.enabled === '1' ? false : true;
    button.disabled = true;
    try {
      const res = await window.studio.scheduleSetEnabled({ id: s.id, enabled: wanted });
      if (!res || res.ok !== true) throw new Error((res && res.error) || '操作失败');
      toast('success', wanted ? '已启用' : '已停用',
        wanted ? '到期会照常触发' : '保留在列表里，不再触发');
      onChanged();
    } catch (e) {
      // 一次性日程的 at 已过时后端会明确拒绝，这里原样转述原因。
      toast('error', wanted ? '启用失败' : '停用失败', String(e.message || e));
      button.disabled = false;
    }
  }

  const whenLine = s.kind === 'recurring'
    ? `下次触发：${s.next_run || '—'} · 规则 ${whenLabel(s)}`
    : `触发时间：${whenLabel(s)}`;
  const lastLine = s.last_run_at ? ` · 上次 ${s.last_run_at}` : '';

  return h('div', { class: 'schedule-card', 'data-schedule': s.id },
    h('div', { class: 'schedule-head' },
      h('span', { class: `schedule-state ${state.cls}` }, state.label),
      h('span', { class: 'schedule-meta mono' },
        `${s.kind === 'recurring' ? '周期' : '一次性'} · ${modeLabel}${s.mock ? ' · Mock' : ''}`),
      h('div', { class: 'schedule-actions' },
        toggle,
        h('button', { class: 'btn btn-sm', onclick: () => onEdit(s) }, '编辑'),
        h('button', { class: 'btn btn-sm btn-danger', onclick: () => {
          confirmAction('删除定时任务', `将删除「${s.goal}」，此操作不可撤销。`, async () => {
            try {
              const res = await window.studio.scheduleRemove({ id: s.id });
              if (!res || res.ok !== true) throw new Error((res && res.error) || '删除失败');
              toast('success', '已删除', '定时任务已移除');
              onChanged();
            } catch (e) { toast('error', '删除失败', String(e.message || e)); }
          }, '删除');
        } }, '删除'))),
    h('div', { class: 'schedule-goal' }, s.goal),
    h('div', { class: 'schedule-when' }, whenLine + lastLine),
    h('div', { class: 'schedule-history mono' }, historyLine));
}

export { scheduleCard };
