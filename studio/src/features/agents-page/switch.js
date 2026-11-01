/**
 * 执行体切换与回滚：把某个角色正在用的 agent 换成另一个，或撤回到配置里的绑定。
 *
 * 走 `POST /runs/{id}/agents`：
 * - `{role, agent}` 切换；`{role, reset: true}` 撤销覆盖（幂等）。
 * - 后端只在 run 仍在跑时热切；否则 409，本模块如实提示，绝不假装成功。
 *
 * 懒加载：只有点了「切换执行体」才去 spawn `main.py agents --json`
 * （那是一次真实进程启动，不该在每次进页面时都付这个代价）。
 */
import { apiFetch } from '../../lib/api.js';
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

let cache = null;

function loadAgents() {
  if (!cache) {
    cache = (async () => {
      if (!window.studio || !window.studio.agentsConfig) throw new Error('当前环境不支持读取绑定');
      const res = await window.studio.agentsConfig();
      if (!res || !res.ok) throw new Error((res && res.error) || '读取绑定失败');
      return res.data || { profiles: [], roles: [] };
    })().catch((e) => { cache = null; throw e; });   // 失败不缓存，允许重试
  }
  return cache;
}

/** 解析某角色当前的 primary 执行体名；拿不到返回空串。 */
function currentProfile(data, role) {
  const row = (data.roles || []).find((r) => r.role === role);
  const primary = row && (row.agents || []).find((a) => a.duty === 'primary');
  return (primary && primary.ref) || '';
}

function setMsg(el, text, kind) {
  el.textContent = text;
  el.className = 'ag-switch-msg' + (kind ? ` ${kind}` : '');
}

/** 把 HTTP 错误翻译成"用户下一步该做什么"，而不是抛原始状态码。 */
function explain(e, profile) {
  if (e.code === 'NO_API') return ['未连接后端：先启动 python main.py serve 并在设置里填地址', 'warn'];
  if (e.code === 'HTTP_409') return ['该 run 不在运行中：用 CLI switch 写入后 resume', 'warn'];
  if (e.code === 'HTTP_400') return [`后端拒绝：${profile || '该操作'} 不被接受`, 'err'];
  return [`请求失败：${e.message}`, 'err'];
}

/** 统一发请求；返回解析后的 JSON，失败时写提示并返回 null。 */
async function postAgents(body, msg) {
  const run = S.dash && S.dash.run;
  const runId = run && run.run_id;
  if (!runId) { setMsg(msg, '当前没有加载 run', 'warn'); return null; }
  try {
    return await apiFetch(`/runs/${encodeURIComponent(runId)}/agents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 10000,
    });
  } catch (e) {
    const [text, kind] = explain(e, body.agent);
    setMsg(msg, text, kind);
    return null;
  }
}

async function doSwitch(role, profile, msg) {
  setMsg(msg, `正在切换 ${role} → ${profile}…`);
  const data = await postAgents({ role, agent: profile, reason: 'studio' }, msg);
  if (!data) return;
  setMsg(msg, `已切换 → ${(data.members || []).join(' + ') || profile}（下次派发生效）`, 'ok');
}

async function doReset(role, msg) {
  setMsg(msg, `正在恢复 ${role} 的默认绑定…`);
  const data = await postAgents({ role, reset: true }, msg);
  if (!data) return;
  const members = (data.members || []).join(' + ') || '角色默认模型';
  if (data.changed) setMsg(msg, `已恢复默认 → ${members}`, 'ok');
  else setMsg(msg, `本来就是默认绑定（${members}）`, 'warn');
}

/**
 * 渲染一行「执行体 [当前] [切换执行体] [恢复默认] 提示」。
 * 只在点按钮时才拉数据，拉不到就地把原因写在提示位，不弹窗打断。
 */
function buildAgentSwitch(role) {
  const msg = h('span', { class: 'ag-switch-msg' }, '');
  const select = h('select', { class: 'ag-switch-select hidden' });
  const resetBtn = h('button', { class: 'btn btn-sm hidden', type: 'button' }, '恢复默认');
  const btn = h('button', { class: 'btn btn-sm', type: 'button' }, '切换执行体');

  resetBtn.addEventListener('click', () => doReset(role, msg));

  btn.addEventListener('click', async () => {
    if (!select.classList.contains('hidden')) {
      await doSwitch(role, select.value, msg);
      return;
    }
    btn.disabled = true;
    setMsg(msg, '读取绑定…');
    try {
      const data = await loadAgents();
      const keys = (data.profiles || []).map((p) => p.key);
      if (!keys.length) { setMsg(msg, '还没有配置执行体档案（agentcrew.yaml 的 agents.profiles）', 'warn'); return; }
      const current = currentProfile(data, role);
      select.innerHTML = '';
      for (const key of keys) {
        select.appendChild(h('option', { value: key }, key + (key === current ? '（当前）' : '')));
      }
      select.value = current || keys[0];
      select.classList.remove('hidden');
      resetBtn.classList.remove('hidden');
      btn.textContent = '确认切换';
      setMsg(msg, current ? `当前：${current}` : '当前未绑定执行体（走角色默认模型）');
    } catch (e) {
      setMsg(msg, `读取绑定失败：${e.message}`, 'err');
    } finally {
      btn.disabled = false;
    }
  });

  return h('div', { class: 'ag-switch' },
    h('span', { class: 'ag-switch-label' }, '执行体'),
    select, btn, resetBtn, msg);
}

export { buildAgentSwitch };
