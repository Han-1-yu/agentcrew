/**
 * 底部悬浮命令输入框：A2 语义分流后的统一入口。
 *
 * 发送行为按当前 run 状态分流（create / guidance / stop），实现见
 * ./submit.js；附件区（A5）与新建运行弹窗共用 features/attachments。
 * 输入框本身不持有业务状态，删掉不影响数据。
 */
import { $ } from '../../lib/dom.js';
import { simplifyGoal } from '../../lib/format.js';
import { resolveComposerIntent } from '../../lib/composer-intent.js';
import { S } from '../../lib/store.js';

import { createAttachmentArea } from '../attachments/index.js';
import { openNewRunModal } from '../new-run/index.js';
import { submitComposer } from './submit.js';

import { initModelMenu } from './model-menu.js';

//: 执行模式（Agent/Plan/Ask）与 new-run 弹窗共用，见 lib/mode.js
import { MODES, currentMode, cycleMode } from '../../lib/mode.js';

let attachments = null;

const PLACEHOLDER_BY_INTENT = {
  create: '描述目标，AgentCrew 会自动编排 Agent 依赖与执行顺序…',
  guidance: '对正在运行的 Crew 说点什么（作为中途指引注入，⌘/Ctrl+Enter 发送）…',
  stop: '规划/准备阶段暂不接受指引，可先停止或等待进入执行…',
};

/** 发送键 / 占位文案随语义分流切换（每秒与输入事件都会调用）。 */
function updateSendState() {
  // 新对话态（A1）恒为 create：即便旧 run 还在跑，这里发的也是新运行
  const run = (S.dash && S.dash.run && !S.newChat) ? S.dash.run : null;
  const intent = resolveComposerIntent({ hasRun: !!run, runStatus: run && run.status, realMode: !!S.realMode });
  S.composerIntent = intent;
  const btn = $('#cmp-send');
  if (btn) {
    const el = $('#cmp-input');
    const stopMode = intent !== 'create' && !(el && el.value.trim());
    btn.classList.toggle('is-stop', stopMode);
    const title = stopMode ? '停止整个运行' : '发送（⌘/Ctrl + Enter）';
    btn.title = title;
    btn.setAttribute('aria-label', title);
  }
  const el = $('#cmp-input');
  if (el) {
    const ph = PLACEHOLDER_BY_INTENT[intent] || PLACEHOLDER_BY_INTENT.create;
    if (el.placeholder !== ph) el.placeholder = ph;
  }
}

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
    // 命令框服务的是「下一次运行」，所以优先显示设置里的默认模型；没有才退回当前运行的模型
    const name = (S.settings && S.settings.defaultModel) || (run && run.model) || '默认模型';
    if (model.textContent !== name) model.textContent = name;
  }
  const modelBtn = $('#cmp-model');
  if (modelBtn) modelBtn.title = `当前模型：${model ? model.textContent : '—'}（点击切换）`;
  updateSendState();
}

/** 发送：语义分流见 ./submit.js（空输入只把焦点还给输入框，不弹错）。 */
function submit(){
  submitComposer(() => (attachments ? attachments.get() : []));
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

  input.addEventListener('input', () => { autoResize(); updateSendState(); });
  input.addEventListener('keydown', (e) => {
    // ⌘/Ctrl + Enter 发送（分流后可能是创建/指引/停止）；单独 Enter 保留换行
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      submit();
    }
  });

  const send = $('#cmp-send');
  if (send) send.addEventListener('click', submit);

  // A5：附件区（＋ 选图，≤4 张）；完整表单仍走 composer-bar / ⌘N
  const attHost = $('#cmp-att');
  if (attHost) {
    attachments = createAttachmentArea({ compact: true });
    attHost.replaceChildren(attachments.node);
  }

  const bar = $('.composer-bar');
  if (bar) bar.addEventListener('click', () => openNewRunModal());

  const access = $('#cmp-access');
  if (access) {
    access.addEventListener('click', () => {
      cycleMode();
      syncComposer();
    });
  }

  // 输入卡右下角的模型名 = 快速切换入口；选中后回写开关文案
  initModelMenu({ onPicked: syncComposer });

  syncComposer();
  autoResize();
}

/** 清空附件（新运行切换后由装配层调用，避免上一轮的图跟着下一次发送）。 */
function resetAttachments() { if (attachments) attachments.clear(); }

export { initComposer, resetAttachments, syncComposer };

