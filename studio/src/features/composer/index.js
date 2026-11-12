/**
 * 底部悬浮命令输入框：新建运行的快捷入口。
 *
 * 它不产生任何数据：只把「一句话目标」交给 openNewRunModal 预填，
 * 真正的启动仍然走新建运行弹窗 → window.studio.startRun 那条既有链路。
 * 这样命令框可以随时删掉而不影响业务。
 */
import { $ } from '../../lib/dom.js';
import { simplifyGoal } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { openNewRunModal } from '../new-run/index.js';

//: 执行模式（Agent/Plan/Ask）与 new-run 弹窗共用，见 lib/mode.js
import { MODES, currentMode, cycleMode } from '../../lib/mode.js';

/** 把当前运行/默认模型/执行模式同步到命令框（每秒由 boot 的定时器调用一次）。 */
function syncComposer(){
  const run = S.dash && S.dash.run;
  const proj = $('#cmp-proj');
  if (proj) {
    const text = (run && run.project_goal) ? simplifyGoal(run.project_goal) : '未选择运行';
    if (proj.textContent !== text) proj.textContent = text;
  }
  const m = MODES[currentMode()];
  const mode = $('#cmp-mode');
  if (mode) mode.textContent = m.mode;
  const label = $('#cmp-access-label');
  if (label) label.textContent = m.access;
  const tag = $('#cmp-access');
  if (tag) {
    tag.classList.toggle('off', currentMode() !== 'agent');
    tag.title = `执行模式：${m.mode}（点击切换 自动执行 → 计划审查 → 只读规划）`;
  }
  const model = $('#cmp-model-name');
  if (model) {
    const name = (run && run.model) ? run.model : '默认模型';
    if (model.textContent !== name) model.textContent = name;
  }
}

/** 发送：有目标才继续，空输入只把焦点还给输入框，不弹错。 */
function submit(){
  const el = $('#cmp-input');
  const goal = el ? el.value.trim() : '';
  if (!goal) {
    if (el) el.focus();
    toast('info', '还没有目标', '先写一句要做什么，AgentCrew 才好编排 Agent');
    return;
  }
  if (el) el.value = '';
  autoResize();
  openNewRunModal({ goal });
}

function autoResize(){
  const el = $('#cmp-input');
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(148, el.scrollHeight) + 'px';
}

function initComposer(){
  const input = $('#cmp-input');
  if (!input) return;

  input.addEventListener('input', autoResize);
  input.addEventListener('keydown', (e) => {
    // ⌘/Ctrl + Enter 启动；单独 Enter 保留换行
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      submit();
    }
  });

  const send = $('#cmp-send');
  if (send) send.addEventListener('click', submit);

  const add = $('#cmp-add');
  if (add) add.addEventListener('click', () => openNewRunModal());

  const bar = $('.composer-bar');
  if (bar) bar.addEventListener('click', () => openNewRunModal());

  const access = $('#cmp-access');
  if (access) {
    access.addEventListener('click', () => {
      cycleMode();
      syncComposer();
    });
  }

  syncComposer();
  autoResize();
}

export { initComposer, syncComposer };
