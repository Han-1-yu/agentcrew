/**
 * 执行体绑定：读取候选、发切换请求、把错误翻成人话。
 *
 * 角色级（`/runs/{id}/agents`）与任务级（`/runs/{id}/tasks/{tid}/agent`）
 * 共用这一层，避免两个页面各写一套超时/错误处理，慢慢走偏。
 */
import { apiFetch } from './api.js';
import { S } from './store.js';

let cache = null;

/** 读 agent 档案与绑定（懒加载 + 失败不缓存）。 */
function loadAgents() {
  if (!cache) {
    cache = (async () => {
      if (!window.studio || !window.studio.agentsConfig) throw new Error('当前环境不支持读取绑定');
      const res = await window.studio.agentsConfig();
      if (!res || !res.ok) throw new Error((res && res.error) || '读取绑定失败');
      return res.data || { profiles: [], roles: [] };
    })().catch((e) => { cache = null; throw e; });
  }
  return cache;
}

/** 某角色当前 primary 执行体名；拿不到返回空串。 */
function currentProfile(data, role) {
  const row = (data.roles || []).find((r) => r.role === role);
  const primary = row && (row.agents || []).find((a) => a.duty === 'primary');
  return (primary && primary.ref) || '';
}

/** 把 HTTP 错误翻成"用户下一步该做什么"。 */
function explain(e, label) {
  if (e.code === 'NO_API') return [`未连接后端：先启动 python main.py serve 并在设置里填地址${e.detail ? `（${e.detail}）` : ''}`, 'warn'];
  if (e.code === 'TRANSPORT') return ['主进程代理不可用：请重启 Studio', 'err'];
  if (e.code === 'HTTP_409') return ['该 run 不在运行中：用 CLI switch 写入后 resume', 'warn'];
  if (e.code === 'HTTP_400') return [e.detail || `后端拒绝：${label || '该操作'} 不被接受`, 'err'];
  return [`请求失败：${e.detail || e.message}`, 'err'];
}

function runId() {
  const run = S.dash && S.dash.run;
  return (run && run.run_id) || '';
}

/** 发一次切换请求；`scope` 决定打到角色级还是任务级端点。 */
async function postAgentChange(body, { taskId = '', label = '' } = {}) {
  const id = runId();
  if (!id) throw Object.assign(new Error('当前没有加载 run'), { code: 'NO_RUN' });
  const path = taskId
    ? `/runs/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}/agent`
    : `/runs/${encodeURIComponent(id)}/agents`;
  try {
    return await apiFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 10000,
    });
  } catch (e) {
    if (e.code === 'NO_RUN') e.detail = `没有加载 run：${label}`;
    e.translated = explain(e, label);
    throw e;
  }
}

export { postAgentChange, currentProfile, explain, loadAgents };
