/**
 * 计划审查页：任务可编辑（标题 / 角色 / 依赖）+ 审批触发。
 */
import { ROLE_ORDER, STATUS_META, roleMeta } from '../../contracts/enums.js';

import { h, s } from '../../lib/dom.js';
import { fmtDur, simplifyGoal } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { readNumber } from '../../lib/validate.js';

import { emptyState } from '../../ui/states.js';
import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { pushEvent } from '../../app/mock.js';
import { switchNav } from '../../app/router.js';

import { renderDashboard } from '../dashboard/index.js';
function renderPlanPage(container) {
  const run = S.dash.run;
  const tasks = S.dash.tasks || [];
  const isReal = !!S.realMode;
  S.planDirty = S.planDirty || false;

  const renderList = () => {
    list.innerHTML = '';
    const sorted = [...tasks].sort((a, b) => a.id.localeCompare(b.id));
    if (!sorted.length) {
      list.appendChild(emptyState('暂无任务',
        '这次运行还没有拆解出任务。启动新运行后可在这里审查任务 DAG。'));
      return;
    }
    for (const t of sorted) {
      const meta = STATUS_META[t.status] || STATUS_META.unknown;
      const rm = roleMeta(t.role);
      const row = h('div', { class: 'plan-task-row' },
        h('div', { class: 'plan-task-head' },
          h('span', { class: 'plan-task-id' }, t.id),
          h('span', { class: `status-chip status-${t.status}` }, meta.label),
          h('span', { class: 'plan-task-role', style: `color:${rm.color}` }, rm.full),
          h('div', { style: 'margin-left:auto;display:flex;gap:4px' },
            !isReal ? h('button', { class: 'btn btn-sm btn-danger', onclick: () => {
                if (tasks.some(x => (x.dependencies || []).includes(t.id))) { toast('error', '无法删除', `${t.id} 仍被其他任务依赖`); return; }
                const idx = tasks.indexOf(t); tasks.splice(idx, 1); S.planDirty = true; renderList(); renderStats();
              } }, '删除') : null)),
        h('div', { class: 'plan-task-body' },
          h('div', { class: 'plan-field' },
            h('label', {}, '标题'),
            h('input', { class: 'plan-input', value: t.title || '', disabled: isReal,
              oninput: (e) => { t.title = e.target.value; S.planDirty = true; } })),
          h('div', { class: 'plan-field' },
            h('label', {}, '描述'),
            h('textarea', { class: 'plan-textarea', rows: 2, disabled: isReal,
              oninput: (e) => { t.description = e.target.value; S.planDirty = true; } }, t.description || t.brief || '')),
          h('div', { class: 'plan-field-row' },
            h('div', { class: 'plan-field' },
              h('label', {}, '负责 Agent'),
              h('select', { class: 'plan-select', disabled: isReal,
                onchange: (e) => { t.role = e.target.value; S.planDirty = true; } },
                ...ROLE_ORDER.map(r => h('option', { value: r, selected: t.role === r }, roleMeta(r).full)))),
            h('div', { class: 'plan-field' },
              h('label', {}, '优先级'),
              h('select', { class: 'plan-select', disabled: isReal,
                onchange: (e) => { t.priority = parseInt(e.target.value); S.planDirty = true; } },
                ...[1, 3, 5, 7, 9].map(p => h('option', { value: p, selected: (t.priority || 5) === p }, `P${p}`)))),
            h('div', { class: 'plan-field' },
              h('label', {}, '超时 (秒)'),
              h('input', { class: 'plan-input', type: 'number', min: '1', max: '86400', step: '1',
                value: t.timeout_seconds || 300, disabled: isReal,
                oninput: (e) => {
                  // 输入框连 min 都没有时 -5 / 999999999 会直接写进任务模型；
                  // 这里统一走 readNumber，非法值只标红、不落盘（不弹 toast 刷屏）。
                  const r = readNumber(e.target, { min: 1, max: 86400, integer: true,
                    fallback: 300, label: '超时' });
                  e.target.classList.toggle('input-error', !!r.error);
                  if (!r.error) { t.timeout_seconds = r.value; S.planDirty = true; }
                } })),
            h('div', { class: 'plan-field' },
              h('label', {}, '依赖'),
              h('input', { class: 'plan-input', value: (t.dependencies || []).join(', '), disabled: isReal,
                placeholder: 'T-001, T-002',
                onchange: (e) => {
                  const deps = e.target.value.split(',').map(s => s.trim()).filter(Boolean);
                  const invalid = deps.filter(d => !tasks.some(x => x.id === d));
                  if (invalid.length) { toast('error', '无效依赖', invalid.join(', ') + ' 不存在'); return; }
                  if (deps.includes(t.id)) { toast('error', '循环依赖', '不能依赖自身'); return; }
                  t.dependencies = deps; S.planDirty = true;
                } })))));
      list.appendChild(row);
    }
  };

  const renderStats = () => {
    const totalTasks = tasks.length;
    const roles = new Set(tasks.map(t => t.role));
    const estCost = tasks.reduce((s, t) => s + ((t.result && t.result.cost_usd) || 0.02), 0);
    const estTime = tasks.reduce((s, t) => s + ((t.result && t.result.duration_ms) || 60000), 0);
    stats.innerHTML = '';
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, String(totalTasks)), h('div', { class: 'plan-stat-label' }, '任务数')));
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, String(roles.size)), h('div', { class: 'plan-stat-label' }, '涉及角色')));
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, `$${estCost.toFixed(3)}`), h('div', { class: 'plan-stat-label' }, '估算费用')));
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, fmtDur(estTime)), h('div', { class: 'plan-stat-label' }, '估算耗时')));
  };

  // 页面头部
  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '计划审查'),
      h('div', { class: 'plan-goal', title: run.project_goal }, simplifyGoal(run.project_goal || '未命名项目'))),
    h('div', { style: 'display:flex;gap:8px;align-items:center' },
      !isReal ? h('button', { class: 'btn', onclick: () => {
          // C-14：用「现有 ID 最大值 + 1」而不是 tasks.length + 1——删除过
          // 任务后 length 会变小，生成出与现存任务重复的 id，导致 DAG 分层、
          // 详情跳转、按 id 查找全部指向错误任务。
          const maxSeq = tasks.reduce((max, t) => {
            const n = parseInt(String(t.id || '').replace(/^T-/, ''), 10);
            return Number.isFinite(n) ? Math.max(max, n) : max;
          }, 0);
          const nextId = 'T-' + String(maxSeq + 1).padStart(3, '0');
          tasks.push({ id: nextId, title: '新任务', description: '', role: 'backend_dev', status: 'pending', priority: 5, timeout_seconds: 300, dependencies: [] });
          S.planDirty = true; renderList(); renderStats();
        } }, '+ 添加任务') : null,
      !isReal ? h('button', { class: 'btn', onclick: () => { S.planDirty = false; toast('success', '已保存', '计划已保存到内存'); } }, '保存计划') : null,
      !isReal ? h('button', { class: 'btn btn-primary', onclick: () => {
          if (!tasks.length) { toast('error', '无法批准', '任务 DAG 为空'); return; }
          const noRole = tasks.filter(t => !t.role);
          if (noRole.length) { toast('error', '无法批准', noRole.map(t => t.id).join(', ') + ' 缺少负责 Agent'); return; }
          confirmAction('批准计划', `共 ${tasks.length} 个任务，涉及 ${new Set(tasks.map(t=>t.role)).size} 个角色。批准后将进入执行阶段。`, () => {
            run.status = 'running'; run.phase = 'execution';
            tasks.forEach(t => { if (t.status === 'pending') t.status = 'ready'; });
            pushEvent('human_approved', { note: 'plan approved', task_count: tasks.length });
            S.planDirty = false; renderDashboard(); toast('success', '计划已批准', '进入执行阶段'); switchNav('dashboard');
          });
        } }, '批准并执行') : null,
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  // 只读提示
  if (isReal) {
    container.appendChild(h('div', { class: 'plan-readonly-banner' },
      '真实模式下计划为只读。如需修改，请通过 CLI 或 API 编辑计划后重新加载。'));
  }

  // 统计栏
  const stats = h('div', { class: 'plan-stats' });
  container.appendChild(stats);
  renderStats();

  // 任务列表
  const list = h('div', { class: 'plan-task-list' });
  container.appendChild(list);
  renderList();
}

export { renderPlanPage };
