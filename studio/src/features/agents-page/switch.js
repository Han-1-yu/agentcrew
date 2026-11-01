/**
 * 执行体切换与回滚（角色级）。
 *
 * 走 `POST /runs/{id}/agents`：
 * - `{role, agent}` 切换；`{role, reset: true}` 撤销覆盖（幂等）。
 * - 后端只在 run 仍在跑时热切；否则 409，本模块如实提示，绝不假装成功。
 *
 * 懒加载：只有点了「切换执行体」才去 spawn `main.py agents --json`
 * （那是一次真实进程启动，不该在每次进页面时都付这个代价）。
 */
import { h } from '../../lib/dom.js';

import { currentProfile, loadAgents, postAgentChange } from '../../lib/agent-binding.js';

function setMsg(el, text, kind) {
  el.textContent = text;
  el.className = 'ag-switch-msg' + (kind ? ` ${kind}` : '');
}

async function send(body, msg, ok) {
  try {
    const data = await postAgentChange(body, { label: body.role });
    ok(data);
  } catch (e) {
    const [text, kind] = e.translated || [`请求失败：${e.message}`, 'err'];
    setMsg(msg, text, kind);
  }
}

async function doSwitch(role, profile, msg) {
  setMsg(msg, `正在切换 ${role} → ${profile}…`);
  await send({ role, agent: profile, reason: 'studio' }, msg, (data) => {
    const members = (data.members || []).join(' + ') || profile;
    setMsg(msg, `已切换 → ${members}（下次派发生效）`, 'ok');
  });
}

async function doReset(role, msg) {
  setMsg(msg, `正在恢复 ${role} 的默认绑定…`);
  await send({ role, reset: true }, msg, (data) => {
    const members = (data.members || []).join(' + ') || '角色默认模型';
    if (data.changed) setMsg(msg, `已恢复默认 → ${members}`, 'ok');
    else setMsg(msg, `本来就是默认绑定（${members}）`, 'warn');
  });
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
