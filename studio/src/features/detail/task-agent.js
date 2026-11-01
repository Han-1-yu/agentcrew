/**
 * 任务详情里的「本次任务执行体」控制：只影响这一个任务，不影响同角色的其他任务。
 *
 * 走 `POST /runs/{id}/tasks/{task_id}/agent`（任务级覆盖优先于角色绑定）。
 * 与角色级切换一样，只在 run 仍在跑时生效；否则如实提示。
 */
import { h } from '../../lib/dom.js';

import { currentProfile, loadAgents, postAgentChange } from '../../lib/agent-binding.js';

function setMsg(el, text, kind) {
  el.textContent = text;
  el.className = 'dp-agent-msg' + (kind ? ` ${kind}` : '');
}

async function send(task, body, msg, ok) {
  try {
    const data = await postAgentChange(
      { ...body, role: task.role }, { taskId: task.id, label: task.id });
    ok(data);
  } catch (e) {
    if (e.code === 'NO_RUN') { setMsg(msg, '当前没有加载 run', 'warn'); return; }
    const [text, kind] = e.translated || [`请求失败：${e.message}`, 'err'];
    setMsg(msg, text, kind);
  }
}

/**
 * 渲染「负责 Agent」下方的一行控制：按钮 → 下拉 → 确认 / 撤销。
 * 任务已经有覆盖时语义仍是"覆盖这次任务"，撤销即回到角色绑定。
 */
function buildTaskAgentControl(task) {
  const msg = h('div', { class: 'dp-agent-msg' }, '');
  const select = h('select', { class: 'dp-agent-select hidden' });
  const undo = h('button', { class: 'btn btn-sm hidden', type: 'button' }, '撤销覆盖');
  const btn = h('button', { class: 'btn btn-sm', type: 'button' }, '本任务换执行体');

  undo.addEventListener('click', async () => {
    setMsg(msg, '正在撤销该任务的覆盖…');
    await send(task, { reset: true }, msg, (data) => {
      if (data.changed) setMsg(msg, `已撤销：${task.id} 回到角色绑定`, 'ok');
      else setMsg(msg, '这个任务本来就没有覆盖', 'warn');
    });
  });

  btn.addEventListener('click', async () => {
    if (!select.classList.contains('hidden')) {
      setMsg(msg, `正在设置 ${task.id} → ${select.value}…`);
      await send(task, { agent: select.value, reason: 'studio-task' }, msg, (data) => {
        setMsg(msg, `已设置：${task.id} 由 ${data.agent} 执行（下次派发生效）`, 'ok');
      });
      return;
    }
    btn.disabled = true;
    setMsg(msg, '读取绑定…');
    try {
      const data = await loadAgents();
      const keys = (data.profiles || []).map((p) => p.key);
      if (!keys.length) { setMsg(msg, '还没有配置执行体档案（agentcrew.yaml 的 agents.profiles）', 'warn'); return; }
      const current = currentProfile(data, task.role);
      select.innerHTML = '';
      for (const key of keys) {
        select.appendChild(h('option', { value: key }, key + (key === current ? '（角色默认）' : '')));
      }
      select.value = current || keys[0];
      select.classList.remove('hidden');
      undo.classList.remove('hidden');
      btn.textContent = '确认';
      setMsg(msg, current ? `角色默认：${current}` : '该角色没有绑定执行体');
    } catch (e) {
      setMsg(msg, `读取绑定失败：${e.message}`, 'err');
    } finally {
      btn.disabled = false;
    }
  });

  // 顺序与阅读顺序一致：先选人，再确认，最后才是撤销
  return h('div', { class: 'dp-agent-ctl' }, select, btn, undo, msg);
}

export { buildTaskAgentControl };
