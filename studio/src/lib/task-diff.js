/**
 * E3 计划 diff：两次 run 的任务表按任务 id 对齐，给出「新增 / 移除 / 变更」。
 *
 * 纯函数（node:test 直测），不碰 DOM。两条取舍：
 * 1. **状态先归一**：引擎历史上 `passed` 与 `succeeded` 是同一档，不归一会把
 *    「同义状态」做成假差异；显示仍用原始值，只有比较用归一值。
 * 2. **只比计划语义**：标题 / 角色 / 状态 / 优先级 / 依赖五项。描述、验收标准、
 *    brief 这类长文本不进 diff——两次 mock 运行的措辞差异全是噪声，会把真正的
 *    计划变更淹掉。
 */

const STATUS_ALIASES = { passed: 'succeeded' };

/** 状态归一：`passed` → `succeeded`，其余原样（去空白）。 */
function normStatus(status) {
  const value = String(status == null ? '' : status).trim();
  return STATUS_ALIASES[value] || value;
}

/** 任务 id 的自然序：有数字尾巴的按数字排（T-2 在 T-10 前），其余按字典序垫后。 */
function cmpTaskId(a, b) {
  const na = String(a).match(/(\d+)(?!.*\d)/);
  const nb = String(b).match(/(\d+)(?!.*\d)/);
  if (na && nb) {
    const d = Number(na[1]) - Number(nb[1]);
    if (d !== 0) return d;
  } else if (na || nb) {
    return na ? -1 : 1;
  }
  return String(a).localeCompare(String(b));
}

/** 依赖集合归一：去掉重复与空项，排序后用逗号连接（顺序不同不算变更）。 */
function normDeps(deps) {
  return [...new Set((deps || []).map((d) => String(d).trim()).filter(Boolean))]
    .sort(cmpTaskId)
    .join(',');
}

function taskSnapshot(task) {
  return {
    id: task.id,
    title: String(task.title || ''),
    role: String(task.role || ''),
    status: String(task.status || ''),
    priority: task.priority,
    dependencies: task.dependencies || [],
  };
}

/** 单任务差异描述：返回可直接展示的中文条目。 */
function describeChanges(a, b) {
  const changes = [];
  if (a.title !== b.title) changes.push(`标题「${a.title || '—'}」→「${b.title || '—'}」`);
  const sa = normStatus(a.status), sb = normStatus(b.status);
  if (sa !== sb) changes.push(`状态 ${sa || '—'}→${sb || '—'}`);
  if (a.role !== b.role) changes.push(`角色 ${a.role || '—'}→${b.role || '—'}`);
  if (Number(a.priority) !== Number(b.priority)) {
    changes.push(`优先级 ${Number(a.priority) || 0}→${Number(b.priority) || 0}`);
  }
  const da = normDeps(a.dependencies), db = normDeps(b.dependencies);
  if (da !== db) changes.push(`依赖 ${da || '—'}→${db || '—'}`);
  return changes;
}

/**
 * 计划 diff 主函数。
 * @param {Array} left  当前 run 的 tasks.json 任务表
 * @param {Array} right 对比 run 的任务表
 * @returns {{rows:Array,added:number,removed:number,changed:number,same:number}}
 */
function diffTaskPlans(left, right) {
  const leftMap = new Map((left || []).map((t) => [t.id, taskSnapshot(t)]));
  const rightMap = new Map((right || []).map((t) => [t.id, taskSnapshot(t)]));
  const ids = [...new Set([...leftMap.keys(), ...rightMap.keys()])].sort(cmpTaskId);
  const rows = [];
  let added = 0, removed = 0, changed = 0, same = 0;
  for (const id of ids) {
    const a = leftMap.get(id);
    const b = rightMap.get(id);
    if (a && !b) {
      removed += 1;
      rows.push({ id, kind: 'removed', left: a, right: null, changes: ['仅当前运行有'] });
      continue;
    }
    if (!a && b) {
      added += 1;
      rows.push({ id, kind: 'added', left: null, right: b, changes: ['仅对比运行有'] });
      continue;
    }
    const changes = describeChanges(a, b);
    if (changes.length) {
      changed += 1;
      rows.push({ id, kind: 'changed', left: a, right: b, changes });
    } else {
      same += 1;
      rows.push({ id, kind: 'same', left: a, right: b, changes: [] });
    }
  }
  return { rows, added, removed, changed, same };
}

/** 一句话摘要：给模态框的小标题用。 */
function diffSummary(diff) {
  if (!diff || !diff.rows.length) return '两次运行都没有任务表';
  const parts = [];
  if (diff.added) parts.push(`${diff.added} 新增`);
  if (diff.removed) parts.push(`${diff.removed} 移除`);
  if (diff.changed) parts.push(`${diff.changed} 变更`);
  if (!parts.length) parts.push(`${diff.same} 项一致`);
  return parts.join(' · ');
}

export { cmpTaskId, diffSummary, diffTaskPlans, normDeps, normStatus };
