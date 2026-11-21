/**
 * A2：输入框语义分流（create / guidance / stop）。
 *
 * 同一根输入框，按当前 run 状态决定行为（判定见 lib/composer-intent.js）：
 *  - 空态 / 终态 / 演示 → 直发创建 run：REST POST /runs 优先，CLI 兜底；
 *    高级选项沿用上次记忆（nrMock / 执行模式 / 设置默认模型与预算）。
 *  - running / awaiting_approval → 直发 guidance：POST 202 乐观入列，
 *    4s 轮询与 GET 列表对账；超时视为「可能已发送」，绝不自动重发。
 *  - 活跃但不可注入（planning / preparing）→ 只能停止整个 run。
 * 发送键在 running 且输入为空时切换为「停止」。
 */
import { apiFetch } from '../../lib/api.js';
import { buildGuidanceBody, currentRunId, explainGuidanceError } from '../../lib/guidance.js';
import { resolveComposerIntent } from '../../lib/composer-intent.js';
import { S } from '../../lib/store.js';

import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { appendGuidanceLocal } from '../conversation/assist.js';
import { launchRun } from '../new-run/launch.js';
import { isTransportError, restCapable } from '../new-run/rest-launch.js';

import { currentMode } from '../../lib/mode.js';

/** 与新建弹窗同一份「上次 Mock 选择」记忆。 */
function rememberedMock() {
  const saved = localStorage.getItem('agentcrew:nrMock');
  return saved == null ? !!(S.settings && S.settings.defaultMock) : saved === '1';
}

/* ---- 停止（run 级语义，文案明示） ---- */

async function stopRun() {
  const run = (S.dash && S.dash.run) || {};
  confirmAction('停止整个运行',
    '停止是 run 级操作：所有 Agent 将在当前任务后终止，未完成任务标记为取消/失败。之后可从检查点恢复或回滚重跑。',
    async () => {
      const rid = currentRunId(run, S.watchDir);
      if (restCapable() && rid) {
        try {
          await apiFetch(`/runs/${encodeURIComponent(rid)}/cancel`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
          });
          toast('info', '已发送停止', '等待引擎优雅收尾…');
          return;
        } catch (err) {
          // 404 = run 不在本服务进程名下（CLI 启动的 run），转 CLI 取消；
          // 其余错误（409 = 已终态）如实报错。
          const code = (err && err.code) || '';
          if (code !== 'HTTP_404' && code !== 'TRANSPORT') {
            toast('error', '停止失败', String((err && (err.detail || err.message)) || err));
            return;
          }
        }
      }
      if (run.dir) {
        try {
          await window.studio.cancelOffline(run.dir);
          toast('info', '已发送取消', '等待 CLI 进程退出…');
        } catch (e) {
          toast('error', '取消失败', String((e && e.message) || e));
        }
      }
    }, '停止运行');
}

/* ---- 运行中追问（A3 入口） ---- */

async function sendGuidance(text) {
  const rid = currentRunId(S.dash && S.dash.run, S.watchDir);
  if (!rid) { toast('error', '无法发送', '当前没有选中的 run'); return false; }
  const body = buildGuidanceBody({ text, author: 'studio' });
  try {
    const r = await apiFetch(`/runs/${encodeURIComponent(rid)}/guidance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 12000,
    });
    appendGuidanceLocal({ id: String((r && r.id) || ''), text, ts: '', status: 'pending', target: (r && r.target) || null });
    toast('success', '已发送', '指引已入队，待引擎吸收（门禁页可管理/撤回）');
    return true;
  } catch (err) {
    if (isTransportError(err)) {
      // 超时但服务端可能已写入：乐观入列 + 交给 4s 对账，不自动重发。
      appendGuidanceLocal({ id: `local-${Date.now()}`, text, ts: '', status: 'pending', optimistic: true });
      toast('info', '已发送（待确认）', '网络超时，结果以指引列表对账为准，不会自动重发');
      return true;
    }
    toast('error', '发送失败', explainGuidanceError(err));
    return false;
  }
}

/* ---- 直发创建（C1 编排：REST 优先，CLI 兜底，绝不双发） ---- */

async function launchCreate(goal, attachments) {
  try {
    await launchRun({
      goal,
      demo: false,
      mock: rememberedMock(),
      mode: currentMode(),
      // 模型 / 预算留空：主进程回落设置页默认（与新建弹窗同口径）
      model: null, maxCost: 0, maxLlmCalls: 0,
      images: attachments && attachments.length ? attachments.map((it) => it.path) : null,
    });
    S.newChat = false;
  } catch (e) {
    toast('error', '启动失败', String((e && e.message) || e));
  }
}

/* ---- 提交入口（composer/index.js 调用） ---- */

function submitComposer(getAttachments) {
  const el = document.getElementById('cmp-input');
  const goal = el ? el.value.trim() : '';
  // 新对话态（A1）恒为 create：即便旧 run 还在跑
  const run = (S.dash && S.dash.run && !S.newChat) ? S.dash.run : null;
  const intent = resolveComposerIntent({ hasRun: !!run, runStatus: run && run.status, realMode: !!S.realMode });

  if (intent === 'guidance' || intent === 'stop') {
    if (!goal) { void stopRun(); return; }
    if (intent === 'stop') {
      toast('info', '暂不能发送指引', 'run 处于规划/准备阶段，引擎尚未开放注入；内容已保留在输入框');
      return;
    }
    void sendGuidance(goal).then((ok) => {
      if (ok && el) { el.value = ''; el.dispatchEvent(new Event('input')); }
    });
    return;
  }

  if (!goal) {
    if (el) el.focus();
    toast('info', '还没有目标', '先写一句要做什么，AgentCrew 才好编排 Agent');
    return;
  }
  const images = typeof getAttachments === 'function' ? getAttachments() : [];
  if (el) { el.value = ''; el.dispatchEvent(new Event('input')); }
  void launchCreate(goal, images);
}

export { launchCreate, stopRun, submitComposer };
