/**
 * F+：流程库页（P1）。R26/R28 后端能力的前端入口——流程的列表 / 查看 /
 * 校验 / 删除 / 从当前 run 另存为流程 / 用此流程启动。
 *
 * 端点：GET /flows、GET /flows/{name}、POST /flows、POST /flows/validate、
 * DELETE /flows/{name}（artifacts/contracts/openapi.yaml 锁定的契约）。
 * 图视图用轻量节点表呈现（id/标题/角色/依赖），不复用 DAG 画布——
 * 画布服务的是「当前 run 的实时状态」，流程库是静态资产，两者数据形状不同。
 */
import { apiFetch } from '../../lib/api.js';
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { confirmAction, closeModal, showModal } from '../../ui/modal.js';
import { emptyState, errorState, loadingState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

import { openNewRunModal } from '../new-run/index.js';

function fmtUpdated(item) {
  const ts = Number(item.updated_at) || 0;
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : '—';
}

function explain(err) {
  return String((err && (err.detail || err.message)) || err || '未知错误');
}

/** 查看：图视图节点表 + 规范化 YAML + 校验错误。 */
async function openFlowDialog(name) {
  showModal(h('div', { class: 'flow-dialog' },
    h('h3', { class: 'modal-title' }, `流程：${name}`),
    h('div', { class: 'flow-dialog-body', id: 'flow-dialog-body' },
      h('div', { class: 'flow-dialog-loading' }, '正在读取…'))));
  let payload;
  try {
    payload = await apiFetch(`/flows/${encodeURIComponent(name)}`);
  } catch (err) {
    const body = document.getElementById('flow-dialog-body');
    if (body) body.replaceChildren(h('div', { class: 'flow-validate-err' }, explain(err)));
    return;
  }
  const view = (payload && payload.view) || {};
  const body = document.getElementById('flow-dialog-body');
  if (!body) return;
  const rows = (view.nodes || []).map((n) => h('tr', {},
    h('td', { class: 'mono' }, n.id),
    h('td', {}, n.title || ''),
    h('td', {}, n.role || ''),
    h('td', { class: 'mono' }, (n.dependencies || []).join(', ') || '—')));
  body.replaceChildren(
    (view.errors || []).length
      ? h('div', { class: 'flow-validate-err' }, `校验错误：${view.errors.join('；')}`)
      : h('div', { class: 'flow-validate-ok' }, '校验通过'),
    view.description ? h('div', { class: 'flow-desc' }, view.description) : null,
    h('table', { class: 'flow-node-table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'ID'), h('th', {}, '任务'), h('th', {}, '角色'), h('th', {}, '依赖'))),
      h('tbody', {}, ...rows)),
    h('pre', { class: 'flow-yaml' }, String(payload.yaml || '')),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '关闭')));
}

/** 新建 / 导入：YAML 文本 → 校验 → 保存。 */
function openImportDialog(onDone) {
  const yaml = h('textarea', {
    class: 'flow-yaml-input', id: 'flow-yaml', rows: 12, spellcheck: 'false',
    placeholder: '粘贴或编写流程 YAML（可用 CLI `python main.py flow sample` 生成模板）',
    'aria-label': '流程 YAML',
  });
  const result = h('div', { class: 'flow-validate-result' });
  const validateBtn = h('button', {
    class: 'btn btn-sm', type: 'button',
    onclick: async () => {
      result.replaceChildren(h('span', { class: 'flow-validate-pending' }, '校验中…'));
      try {
        const r = await apiFetch('/flows/validate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml: yaml.value }), timeoutMs: 15000,
        });
        if (r && r.ok) {
          result.replaceChildren(h('div', { class: 'flow-validate-ok' },
            `校验通过（${(r.view && r.view.nodes || []).length} 个任务），可保存`));
        } else {
          result.replaceChildren(h('div', { class: 'flow-validate-err' },
            `校验失败：${((r && r.errors) || ['未知错误']).join('；')}`));
        }
      } catch (err) {
        result.replaceChildren(h('div', { class: 'flow-validate-err' }, explain(err)));
      }
    },
  }, '校验');
  const saveBtn = h('button', {
    class: 'btn btn-primary btn-sm', type: 'button',
    onclick: async () => {
      saveBtn.disabled = true;
      try {
        const r = await apiFetch('/flows', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml: yaml.value }), timeoutMs: 15000,
        });
        toast('success', '已保存流程', String((r && r.saved) || ''));
        closeModal();
        if (typeof onDone === 'function') onDone();
      } catch (err) {
        toast('error', '保存失败', explain(err));
        saveBtn.disabled = false;
      }
    },
  }, '保存到流程库');
  showModal(h('div', {},
    h('h3', { class: 'modal-title' }, '新建 / 导入流程'),
    h('div', { class: 'confirm-text', style: 'color:var(--text-ter)' },
      '保存前会先校验（图不合法不落盘）；保存位置 <output>/_flows/<name>.yaml。'),
    yaml, result,
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '取消'),
      validateBtn, saveBtn)));
  yaml.focus();
}

/** 从当前 run 另存为流程：tasks.json 的任务表 → FlowSpec → 校验落盘。 */
async function saveRunAsFlow() {
  const tasks = (S.dash && S.dash.tasks) || [];
  if (!tasks.length) { toast('info', '无法另存', '当前运行没有任务表'); return; }
  const name = window.prompt('流程名称（保存为 _flows/<name>.yaml）',
    `from-${String((S.dash.run && S.dash.run.run_id) || 'run').slice(0, 40)}`);
  if (!name || !name.trim()) return;
  const spec = {
    version: 1,
    name: name.trim(),
    description: `从 run ${(S.dash.run && S.dash.run.run_id) || ''} 的任务表另存`,
    tasks: tasks.map((t) => ({
      id: t.id, title: t.title || t.id, role: t.role || 'project_manager',
      description: t.description || t.brief || '',
      dependencies: t.dependencies || [],
      priority: t.priority || 5,
    })),
  };
  try {
    const r = await apiFetch('/flows', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flow: spec }), timeoutMs: 15000,
    });
    toast('success', '已另存为流程', String((r && r.saved) || name.trim()));
    closeModal();
  } catch (err) {
    toast('error', '另存失败', explain(err));
  }
}

function flowCard(item, { onView, onStart, onDelete }) {
  const bad = Number(item.errors) > 0;
  return h('div', { class: 'flow-card' },
    h('div', { class: 'flow-card-head' },
      h('span', { class: 'flow-card-name mono' }, item.name),
      h('span', { class: 'flow-card-meta' },
        `${item.tasks || 0} 个任务 · ${fmtUpdated(item)}`),
      bad ? h('span', { class: 'flow-card-badge err' }, '校验错误') : null,
      !bad ? h('span', { class: 'flow-card-badge ok' }, '可用') : null),
    item.error ? h('div', { class: 'flow-card-err' }, item.error) : null,
    h('div', { class: 'flow-card-actions' },
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => onView(item) }, '查看'),
      bad ? null : h('button', { class: 'btn btn-sm btn-primary', type: 'button', onclick: () => onStart(item) }, '用此流程启动'),
      h('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => onDelete(item) }, '删除')));
}

async function load(box, { token }) {
  box.innerHTML = '';
  box.appendChild(loadingState('正在读取流程库…'));
  let flows;
  try {
    const payload = await apiFetch('/flows');
    flows = (payload && payload.flows) || [];
  } catch (err) {
    if (token !== S.navToken) return;
    box.innerHTML = '';
    box.appendChild(errorState('流程库读取失败', explain(err), () => renderFlowsPage(document.getElementById('placeholder-view'))));
    return;
  }
  if (token !== S.navToken || !box.isConnected) return;
  box.innerHTML = '';
  if (!flows.length) {
    box.appendChild(emptyState('流程库还是空的',
      '流程 = 预定义的任务表（R28 直通：跳过 PM 规划按表执行）。可从 YAML 导入，或把当前 run 的任务表另存为流程。',
      { label: '新建 / 导入流程', onclick: () => openImportDialog(() => renderFlowsPage(document.getElementById('placeholder-view'))) }));
    return;
  }
  const list = h('div', { class: 'flow-list' });
  for (const item of flows) {
    list.appendChild(flowCard(item, {
      onView: (it) => void openFlowDialog(it.name),
      onStart: (it) => { openNewRunModal({ flowName: it.name }); },
      onDelete: (it) => {
        confirmAction('删除流程', `确认删除流程「${it.name}」？运行中的 run 不受影响（它们用的是保存时的任务表快照）。`, async () => {
          try {
            await apiFetch(`/flows/${encodeURIComponent(it.name)}`, { method: 'DELETE' });
            toast('success', '已删除', it.name);
            renderFlowsPage(document.getElementById('placeholder-view'));
          } catch (err) { toast('error', '删除失败', explain(err)); }
        }, '删除');
      },
    }));
  }
  box.appendChild(list);
}

function renderFlowsPage(container) {
  const base = S.settings && S.settings.apiBaseUrl;
  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '流程库'),
      h('div', { class: 'plan-goal' }, '预定义任务表（R28）：启动运行时选择流程可跳过 PM 规划，按表直接执行'))));
  if (!S.realMode || !base) {
    container.appendChild(emptyState('需要连接后端 API',
      '流程库通过 REST 读写 <output>/_flows/：请在「设置 → 连接」配置后端地址。'));
    return;
  }
  const tools = h('div', { class: 'flow-tools' },
    h('button', {
      class: 'btn btn-sm', type: 'button',
      onclick: () => openImportDialog(() => renderFlowsPage(document.getElementById('placeholder-view'))),
    }, '新建 / 导入'),
    h('button', {
      class: 'btn btn-sm', type: 'button', title: '把当前 run 的任务表保存为可复用流程',
      onclick: () => void saveRunAsFlow(),
    }, '从当前 run 另存为流程'),
    h('button', {
      class: 'btn btn-sm', type: 'button',
      onclick: () => renderFlowsPage(document.getElementById('placeholder-view')),
    }, '刷新'));
  container.appendChild(tools);
  const box = h('div', { class: 'flows-box' });
  container.appendChild(box);
  load(box, { token: S.navToken }).catch((err) => {
    box.innerHTML = '';
    box.appendChild(errorState('流程库读取失败', explain(err)));
  });
}

export { renderFlowsPage };
