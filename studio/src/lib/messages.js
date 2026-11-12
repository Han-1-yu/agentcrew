/**
 * 对话流消息聚合（纯函数，零依赖，便于 node:test 单测）。
 *
 * 把 ledger 事件（信封 ``{ ts, run_id, event, data }``）按时间顺序重建成
 * WorkBuddy 式消息流：``run_started`` → user 目标；``plan_*`` / ``run_*`` /
 * ``budget_*`` / ``error`` → system 消息；其余带 ``task_id`` 的 → 按 task_id
 * 聚合为 agent 消息（内部 items 记录 note/tool/gate/artifact/finish）。
 */
function buildMessages(events, tasks, run) {
  events = events || [];
  tasks = tasks || [];
  run = run || {};
  const taskIndex = {};
  tasks.forEach((t) => { taskIndex[t.id] = t; });

  const msgs = [];
  const taskMsg = new Map();
  let seq = 0;

  for (const e of events) {
    const d = e.data || {};
    const order = seq++;
    const name = e.event;

    if (name === 'run_started') {
      msgs.push({ type: 'user', ts: e.ts, order, goal: d.goal || run.project_goal || '' });
    } else if (name === 'plan_created') {
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'plan', text: `已拆解为 ${d.task_count} 个任务，将由 ${(d.roles || []).length} 个角色协作完成` });
    } else if (name === 'plan_scored') {
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'plan', text: `计划质量评分 ${Math.round((d.total || 0) * 100)} 分` });
    } else if (name === 'plan_rejected') {
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'warn', text: '计划校验未通过，正在重新拆解' });
    } else if (name === 'run_finished') {
      const s = d.status || run.status || '';
      msgs.push({ type: 'system', ts: e.ts, order, kind: s === 'completed' ? 'done' : (s === 'failed' ? 'error' : ''), text: `运行结束${s ? '（' + s + '）' : ''}` });
    } else if (name === 'run_paused' || name === 'approval_requested') {
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'warn', text: '等待人工审批' });
    } else if (name === 'budget_exhausted') {
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'warn', text: '预算耗尽，运行优雅收尾' });
    } else if (name === 'error') {
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'error', text: `错误：${d.message || d.exc_type || ''}` });
    } else {
      const tid = d.task_id || d.taskId;
      if (!tid) continue;
      let tm = taskMsg.get(tid);
      if (!tm) {
        tm = { type: 'agent', ts: e.ts, order, taskId: tid, role: d.role || '', items: [] };
        taskMsg.set(tid, tm);
        msgs.push(tm);
      }
      if (d.role) tm.role = d.role;
      if (name === 'task_notes' && d.notes) tm.items.push({ kind: 'note', text: d.notes, ts: e.ts });
      else if (name === 'tool_call') tm.items.push({ kind: 'tool', tool: d.tool_name, args: d.args, ok: d.success, dur: d.duration_ms, ts: e.ts });
      else if (name === 'gate_result') tm.items.push({ kind: 'gate', layer: d.layer, verdict: d.verdict, score: d.score, issues: d.issues, ts: e.ts });
      else if (name === 'artifact_written') tm.items.push({ kind: 'artifact', path: d.path, size: d.size_bytes, ts: e.ts });
      else if (name === 'task_finished') tm.items.push({ kind: 'finish', dur: d.duration_ms, ts: e.ts });
    }
  }
  msgs.sort((a, b) => a.order - b.order);
  return { msgs, taskIndex };
}

export { buildMessages };
