/**
 * 定时任务页（对齐 WorkBuddy 的 Automations）：一次性 / 周期日程的列表、新建与删除。
 * 到期触发由主进程每分钟调用 `main.py schedule tick` 完成（幂等）。
 */
import { h } from '../../lib/dom.js';

import { MODES, MODE_ORDER } from '../../lib/mode.js';
import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

const WEEKDAYS = [
  ['MO', '一'], ['TU', '二'], ['WE', '三'], ['TH', '四'],
  ['FR', '五'], ['SA', '六'], ['SU', '日'],
];

function emptyState(msg) {
  return h('div', { class: 'page-state' },
    h('div', { class: 'page-state-title' }, '还没有定时任务'),
    h('div', { class: 'page-state-detail' }, msg || '在下方新建一条日程，到点后会自动跑一次运行。'));
}

async function refresh(listEl) {
  listEl.innerHTML = '';
  let schedules = [];
  try {
    const res = await window.studio.scheduleList();
    if (!res || !res.ok) throw new Error(res && res.error || '读取失败');
    schedules = (res.data && res.data.schedules) || [];
  } catch (e) {
    listEl.appendChild(h('div', { class: 'page-state is-error' },
      h('div', { class: 'page-state-title' }, '读取失败'),
      h('div', { class: 'page-state-detail' }, String(e.message || e))));
    return;
  }
  if (!schedules.length) { listEl.appendChild(emptyState()); return; }
  schedules.forEach(s => listEl.appendChild(scheduleCard(s, listEl)));
}

async function ensureScheduleOk(promise, fallback) {
  const result = await promise;
  if (!result || result.ok !== true) throw new Error((result && result.error) || fallback);
  return result;
}

function scheduleCard(s, listEl) {
  const when = s.kind === 'recurring'
    ? `每周 ${(s.weekdays || []).join('·')} ${s.time || ''}`
    : (s.next_run || s.at || '—');
  const modeLabel = (MODES[s.mode] && MODES[s.mode].mode) || s.mode;
  return h('div', { class: 'schedule-card' },
    h('div', { class: 'schedule-head' },
      h('span', { class: `schedule-state ${s.enabled ? 'on' : 'off'}` }, s.enabled ? '启用' : '已触发/停用'),
      h('span', { class: 'schedule-meta mono' }, `${s.kind === 'recurring' ? '周期' : '一次性'} · ${modeLabel}${s.mock ? ' · Mock' : ''}`),
      h('button', { class: 'btn btn-sm btn-danger', onclick: () => {
        confirmAction('删除定时任务', `将删除「${s.goal}」，此操作不可撤销。`, async () => {
          try {
            await ensureScheduleOk(window.studio.scheduleRemove({ id: s.id }), '删除失败');
            toast('success', '已删除', '定时任务已移除');
            refresh(listEl);
          } catch (e) { toast('error', '删除失败', String(e.message || e)); }
        }, '删除');
      } }, '删除')),
    h('div', { class: 'schedule-goal' }, s.goal),
    h('div', { class: 'schedule-when' }, `下次触发：${s.next_run || '—'}`));
}

function buildAddCard(listEl) {
  const kindInputs = {};
  const card = h('div', { class: 'dash-block' },
    h('div', { class: 'block-head' },
      h('h2', { class: 'block-title' }, '新建定时任务'),
      h('div', { class: 'schedule-kind', role: 'tablist' },
        ['once', 'recurring'].map(k => {
          kindInputs[k] = h('button', {
            type: 'button', class: 'schedule-kind-btn' + (k === 'once' ? ' active' : ''),
            'data-kind': k, onclick: () => switchKind(k),
          }, k === 'once' ? '一次性' : '周期');
          return kindInputs[k];
        }))),
    h('div', { class: 'schedule-form' },
      h('div', { class: 'schedule-row' },
        h('label', { class: 'nr-label' }, '目标'),
        h('textarea', { id: 'sched-goal', class: 'nr-textarea', rows: 2, placeholder: '到点要执行的目标，例如：生成每日数据日报' })),
      h('div', { class: 'schedule-row', id: 'sched-once-row' },
        h('label', { class: 'nr-label' }, '触发时间'),
        h('input', { id: 'sched-at', class: 'nr-input', type: 'datetime-local' })),
      h('div', { class: 'schedule-row', id: 'sched-recur-row', style: 'display:none' },
        h('label', { class: 'nr-label' }, '每天触发时间'),
        h('div', { class: 'schedule-recur' },
          h('input', { id: 'sched-time', class: 'nr-input', type: 'time', value: '09:00' }),
          h('div', { class: 'schedule-weekdays' }, WEEKDAYS.map(([k, label]) =>
            h('label', { class: 'schedule-wd' },
              h('input', { type: 'checkbox', 'data-wd': k, checked: k !== 'SA' && k !== 'SU' }), label))))),
      h('div', { class: 'schedule-row' },
        h('label', { class: 'nr-label' }, '执行模式'),
        h('select', { id: 'sched-mode', class: 'nr-select' },
          MODE_ORDER.map(m => h('option', { value: m, selected: m === 'agent' }, MODES[m].mode)))),
      h('label', { class: 'nr-slow-label', style: 'visibility:visible' },
        h('input', { id: 'sched-mock', type: 'checkbox' }), 'Mock 模式（不调大模型）')),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn btn-primary', onclick: async () => {
        const goal = document.getElementById('sched-goal').value.trim();
        if (!goal) { toast('error', '还没有目标', '先写一句到点要执行的目标'); return; }
        const kind = document.querySelector('.schedule-kind-btn.active')?.dataset.kind || 'once';
        const mode = document.getElementById('sched-mode').value;
        const mock = document.getElementById('sched-mock').checked;
        const payload = { goal, mode, mock };
        if (kind === 'once') {
          const at = document.getElementById('sched-at').value;
          if (!at) { toast('error', '缺少时间', '请选择触发时间'); return; }
          payload.at = at.includes('T') && at.length === 16 ? at + ':00' : at;
        } else {
          const time = document.getElementById('sched-time').value;
          const wds = [...document.querySelectorAll('#sched-recur-row input[data-wd]:checked')].map(i => i.dataset.wd);
          if (!time) { toast('error', '缺少时间', '请选择每天触发时间'); return; }
          if (!wds.length) { toast('error', '缺少星期', '至少勾选一个星期几'); return; }
          payload.daily = time; payload.weekdays = wds.join(',');
        }
        try {
          await ensureScheduleOk(window.studio.scheduleAdd(payload), '添加失败');
          toast('success', '已添加', '定时任务已注册');
          refresh(listEl);
        } catch (e) { toast('error', '添加失败', String(e.message || e)); }
      } }, '添加')));

  function switchKind(kind) {
    Object.keys(kindInputs).forEach(k => kindInputs[k].classList.toggle('active', k === kind));
    document.getElementById('sched-once-row').style.display = kind === 'once' ? '' : 'none';
    document.getElementById('sched-recur-row').style.display = kind === 'recurring' ? '' : 'none';
  }
  return card;
}

async function renderSchedulesPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '定时任务'),
    h('button', { class: 'btn', onclick: async () => {
      try {
        const result = await window.studio.scheduleTick();
        if (!result || result.ok !== true) throw new Error((result && result.error) || '检查失败');
        toast('info', '检查完成', result.data && result.data.triggered ? `已触发 ${result.data.triggered} 条到期日程` : '没有到期的日程');
      }
      catch (e) { toast('error', '检查失败', String(e.message || e)); }
    } }, '立即检查到期')));
  const wrap = h('div', { class: 'schedules-wrap' });
  container.appendChild(wrap);
  const listEl = h('div', { id: 'schedules-list' });
  wrap.appendChild(buildAddCard(listEl));
  wrap.appendChild(h('h2', { class: 'block-title', style: 'margin:20px 0 8px' }, '已注册日程'));
  wrap.appendChild(listEl);
  refresh(listEl);
}

export { renderSchedulesPage };
