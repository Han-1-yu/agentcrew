/**
 * 定时任务页（对齐 WorkBuddy 的 Automations）：日程列表 + 新建 / 编辑表单。
 * 到期触发由主进程每分钟调用 `main.py schedule tick` 完成（幂等）。
 */
import { h } from '../../lib/dom.js';
import { toast } from '../../ui/toast.js';

import { scheduleCard } from './card.js';
import { buildScheduleForm } from './form.js';

function emptyState(msg) {
  return h('div', { class: 'page-state' },
    h('div', { class: 'page-state-title' }, '还没有定时任务'),
    h('div', { class: 'page-state-detail' }, msg || '在下方新建一条日程，到点后会自动跑一次运行。'));
}

async function loadTriggers() {
  // 历史读失败不该拖垮列表：卡片退回「扫描范围」文案即可。
  try {
    const res = await window.studio.scheduleHistory();
    if (res && res.ok && res.data && Array.isArray(res.data.triggers)) return res.data.triggers;
  } catch { /* 忽略：没有历史比整页报错更可用 */ }
  return [];
}

async function refresh(listEl, formSlot) {
  listEl.innerHTML = '';
  // 列表要等 CLI（另起 python 进程）返回，所以不再顺带重置表单：
  // 表单在挂载时同步渲染，刷列表不该把用户正在填的内容清掉。
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
  const triggers = await loadTriggers();
  const bySchedule = new Map();
  triggers.forEach(t => {
    if (!bySchedule.has(t.scheduleId)) bySchedule.set(t.scheduleId, []);
    bySchedule.get(t.scheduleId).push(t);
  });
  schedules.forEach(s => listEl.appendChild(scheduleCard(s, {
    triggers: bySchedule.get(s.id) || [],
    onChanged: () => refresh(listEl, formSlot),
    onEdit: (schedule) => {
      renderForm(formSlot, listEl, schedule);
      formSlot.scrollIntoView({ block: 'nearest' });
    },
  })));
}

function renderForm(formSlot, listEl, initial) {
  formSlot.innerHTML = '';
  formSlot.appendChild(buildScheduleForm({
    initial,
    // 提交成功后表单回到「新建」空态（编辑态引用的那条可能已经被替换掉）。
    onDone: () => { renderForm(formSlot, listEl, null); refresh(listEl, formSlot); },
  }));
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
  const formSlot = h('div', { class: 'schedule-form-slot', id: 'schedules-form' });
  const listEl = h('div', { class: 'schedules-list', id: 'schedules-list' });
  wrap.appendChild(formSlot);
  wrap.appendChild(h('h2', { class: 'block-title', style: 'margin:20px 0 8px' }, '已注册日程'));
  wrap.appendChild(listEl);
  // 表单先同步渲染（不等 CLI），列表后台加载：首屏不该被一次子进程启动卡住。
  renderForm(formSlot, listEl, null);
  refresh(listEl, formSlot);
}

export { renderSchedulesPage };
