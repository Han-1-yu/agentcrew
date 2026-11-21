/**
 * 定时任务表单（新建 / 编辑共用）。编辑不走「原地改一份 JSON」：后端没有 update
 * 子命令，前端按「先建新、再删旧」闭环——两步都成功才算完成，删旧失败要如实告诉
 * 用户「新的一条已生效、旧的还在」，而不是报一句笼统的失败。
 */
import { h } from '../../lib/dom.js';
import { MODES, MODE_ORDER } from '../../lib/mode.js';
import {
  WEEKDAY_KEYS, draftFromSchedule, payloadFromDraft,
} from '../../lib/schedule-draft.js';
import { toast } from '../../ui/toast.js';

const WEEKDAY_LABELS = { MO: '一', TU: '二', WE: '三', TH: '四', FR: '五', SA: '六', SU: '日' };

function label(text, control) {
  return h('div', { class: 'schedule-row' }, h('label', { class: 'nr-label' }, text), control);
}

/** 建一个表单卡；`initial` 非空即编辑模式（预填并显示「保存修改」）。 */
function buildScheduleForm({ initial = null, onDone = () => {} } = {}) {
  const draft = draftFromSchedule(initial);
  const editing = !!initial;
  // 表单控件直接持有引用，不再靠 document.getElementById 全局抓取：
  // 编辑一张 + 新建一张同时存在时，id 会打架。
  const goalInput = h('textarea', {
    class: 'nr-textarea', rows: 2, placeholder: '到点要执行的目标，例如：生成每日数据日报',
  });
  goalInput.value = draft.goal;
  const atInput = h('input', { class: 'nr-input', type: 'datetime-local' });
  atInput.value = draft.at;
  const timeInput = h('input', { class: 'nr-input', type: 'time' });
  timeInput.value = draft.time;
  const modeSelect = h('select', { class: 'nr-select' },
    MODE_ORDER.map(m => h('option', { value: m, selected: m === draft.mode }, MODES[m].mode)));
  const mockInput = h('input', { type: 'checkbox' });
  mockInput.checked = draft.mock;

  const onceRow = label('触发时间', atInput);
  const wdInputs = {};
  const recurRow = label('每天触发时间', h('div', { class: 'schedule-recur' },
    timeInput,
    h('div', { class: 'schedule-weekdays' }, WEEKDAY_KEYS.map(k => {
      wdInputs[k] = h('input', { type: 'checkbox', 'data-wd': k });
      wdInputs[k].checked = draft.weekdays.includes(k);
      return h('label', { class: 'schedule-wd' }, wdInputs[k], WEEKDAY_LABELS[k] || k);
    }))));

  let kind = draft.kind;
  const kindButtons = {};
  const submitBtn = h('button', {
    class: 'btn btn-primary', onclick: () => submit(),
  }, editing ? '保存修改' : '添加');
  const card = h('div', { class: 'dash-block' },
    h('div', { class: 'block-head' },
      h('h2', { class: 'block-title' }, editing ? '编辑定时任务' : '新建定时任务'),
      h('div', { class: 'schedule-kind', role: 'tablist' },
        ['once', 'recurring'].map(k => {
          kindButtons[k] = h('button', {
            type: 'button', class: 'schedule-kind-btn',
            'data-kind': k, onclick: () => switchKind(k),
          }, k === 'once' ? '一次性' : '周期');
          return kindButtons[k];
        }))),
    h('div', { class: 'schedule-form' },
      label('目标', goalInput), onceRow, recurRow,
      label('执行模式', modeSelect),
      h('label', { class: 'nr-slow-label', style: 'visibility:visible' }, mockInput,
        'Mock 模式（不调大模型）')),
    h('div', { class: 'modal-actions' }, submitBtn, editing
      ? h('button', { class: 'btn', onclick: () => onDone() }, '取消')
      : null));

  function switchKind(next) {
    kind = next;
    Object.keys(kindButtons).forEach(k => kindButtons[k].classList.toggle('active', k === next));
    onceRow.style.display = next === 'once' ? '' : 'none';
    recurRow.style.display = next === 'recurring' ? '' : 'none';
  }

  async function submit() {
    const current = {
      goal: goalInput.value, kind, mode: modeSelect.value, mock: mockInput.checked,
      at: atInput.value, time: timeInput.value,
      weekdays: WEEKDAY_KEYS.filter(k => wdInputs[k].checked),
    };
    const { payload, error } = payloadFromDraft(current);
    if (error) { toast('error', '还不能提交', error); return; }
    submitBtn.disabled = true;
    try {
      await addThen(payload);
    } finally { submitBtn.disabled = false; }
  }

  async function addThen(payload) {
    const added = await window.studio.scheduleAdd(payload);
    if (!added || added.ok !== true) {
      toast('error', editing ? '改不了' : '添加失败', (added && added.error) || '后端没有返回成功');
      return;
    }
    if (editing) {
      // 「先建新再删旧」：删旧失败时新的一条已经生效，必须按这个事实说话。
      const removed = await window.studio.scheduleRemove({ id: initial.id });
      if (!removed || removed.ok !== true) {
        toast('warning', '已新建，旧项没删掉', String((removed && removed.error) || '旧的日程仍在列表里，请手动删除'));
      } else {
        toast('success', '已更新', '新的一条已生效，旧条目已移除');
      }
    } else {
      toast('success', '已添加', '定时任务已注册');
    }
    onDone();
  }

  switchKind(kind);
  // 编辑模式初始就按原 kind 摆好控件显隐（switchKind 内部会同步按钮态）。
  submitBtn.disabled = false;
  return card;
}

export { buildScheduleForm };
