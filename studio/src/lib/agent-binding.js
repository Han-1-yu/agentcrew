/**
 * 执行体绑定：读取候选、发切换请求、把错误翻成人话。
 *
 * 角色级（`/runs/{id}/agents`）与任务级（`/runs/{id}/tasks/{tid}/agent`）
 * 共用这一层，避免两个页面各写一套超时/错误处理，慢慢走偏。
 */
import { apiFetch } from './api.js';
import { S } from './store.js';

let cache = null;
let usageCache = null;

/** 读 agent 档案与绑定（懒加载 + 失败不缓存）。 */
function loadAgents({ force = false } = {}) {
  if (force) cache = null;
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

/** 档案写操作后让读取缓存失效：下一次进页面 / 刷新拿到新配置。 */
function invalidateAgents() {
  cache = null;
  usageCache = null;
}

function unwrap(res, fallback) {
  if (!res || !res.ok) throw new Error((res && res.error) || fallback);
  return res.data;
}

/** 保存一份或多份档案（text 为 YAML；JSON 也合法）。 */
async function saveAgents(text, { force = false, dryRun = false } = {}) {
  if (!window.studio || !window.studio.agentsSave) throw new Error('当前环境不支持保存档案');
  const data = unwrap(
    await window.studio.agentsSave({ text, force, dryRun }), '保存失败');
  invalidateAgents();
  return data;
}

/** 删除 agents.d 里的档案（后端移入 .trash）。 */
async function deleteAgent(key, { force = false } = {}) {
  if (!window.studio || !window.studio.agentsDelete) throw new Error('当前环境不支持删除档案');
  const data = unwrap(await window.studio.agentsDelete({ key, force }), '删除失败');
  invalidateAgents();
  return data;
}

/** 单个档案的 YAML 片段（复制 / 分享）。 */
async function exportAgentYaml(key) {
  if (!window.studio || !window.studio.agentsYaml) throw new Error('当前环境不支持导出');
  const res = await window.studio.agentsYaml({ key });
  if (!res || !res.ok) throw new Error((res && res.error) || '导出失败');
  return String(res.yaml || '');
}

/** 跨 run 用量（按执行体聚合）；只扫最近 limit 个 run。 */
function loadUsage({ force = false, limit = 30 } = {}) {
  if (force) usageCache = null;
  if (!usageCache) {
    usageCache = (async () => {
      if (!window.studio || !window.studio.agentsUsage) return { profiles: {} };
      const res = await window.studio.agentsUsage({ limit });
      if (!res || !res.ok) throw new Error((res && res.error) || '读取用量失败');
      return res.data || { profiles: {} };
    })().catch((e) => { usageCache = null; throw e; });
  }
  return usageCache;
}

/** 工具目录（编辑器里的白名单候选）；失败返回空表，不阻断编辑。 */
async function loadToolCatalog() {
  if (!window.studio || !window.studio.agentsTools) return { tools: [] };
  try {
    const res = await window.studio.agentsTools();
    return res && res.ok ? (res.data || { tools: [] }) : { tools: [] };
  } catch {
    return { tools: [] };
  }
}

/** 配置读不出来时的兜底诊断（`agents --check` 会尽量给出逐条原因）。 */
async function loadDiagnostics() {
  if (!window.studio || !window.studio.agentsCheck) return null;
  try {
    const res = await window.studio.agentsCheck();
    return res && res.ok ? (res.data || null) : null;
  } catch {
    return null;
  }
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

export {
  deleteAgent, exportAgentYaml, invalidateAgents, loadAgents, loadDiagnostics,
  loadToolCatalog, loadUsage, postAgentChange, currentProfile, explain, saveAgents,
};
