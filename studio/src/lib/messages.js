/**
 * 对话流消息聚合（纯函数，零依赖，便于 node:test 单测）。
 *
 * 把 ledger 事件（信封 ``{ ts, run_id, event, data }``）按时间顺序重建成
 * WorkBuddy 式消息流：``run_started`` → user 目标；``plan_*`` / ``run_*`` /
 * ``budget_*`` / ``error`` / 指引审计事件 → system 消息；``guidance`` 附加
 * 列表（GET /runs/{id}/guidance + 乐观插入）→ user 指引气泡；其余带
 * ``task_id`` 的 → 按 task_id 聚合为 agent 消息（内部 items 记录
 * note/tool/gate/artifact/finish）。
 */
function buildMessages(events, tasks, run, extra) {
  events = events || [];
  tasks = tasks || [];
  run = run || {};
  const taskIndex = {};
  tasks.forEach((t) => { taskIndex[t.id] = t; });

  // 指引附加层：GET 列表 + 乐观插入的项；ledger 只记 id 级审计，
  // 正文要从这里对上（A3）。
  const guidanceItems = (extra && Array.isArray(extra.guidance)) ? extra.guidance : [];
  const guidanceById = new Map();
  for (const g of guidanceItems) {
    if (g && g.id) guidanceById.set(String(g.id), g);
  }

  const msgs = [];
  const taskMsg = new Map();
  let seq = 0;

  for (const e of events) {
    const d = e.data || {};
    const order = seq++;
    const name = e.event;

    if (name === 'run_started') {
      const images = (extra && Array.isArray(extra.images)) ? extra.images.slice(0, 4) : null;
      msgs.push({ type: 'user', ts: e.ts, order, goal: d.goal || run.project_goal || '', images });
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
      // §5：审批单在对话里就地可裁决。只有 approval_requested 带 id 与 checkpoint
      // （run_paused 紧挨着它出现，给同一张卡会造成两处重复加载）。
      const approval = name === 'approval_requested'
        ? { requestId: String(d.approval_id || ''), checkpoint: String(d.checkpoint || '') }
        : null;
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'warn', text: '等待人工审批', approval });
    } else if (name === 'approval_decided') {
      const act = String(d.action || '');
      const verdict = { approve: '批准执行', accept: '接受交付', cancel: '取消运行', abort: '放弃交付' }[act] || act;
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'done', text: `审批已裁决：${verdict}` });
    } else if (name === 'guidance_injected') {
      // 命中附加层的指引只更新气泡的「被吸收」chip；正文不在 ledger 里，
      // 全部未命中（别的客户端发的）才补一条 system 消息，避免双份展示。
      const ids = Array.isArray(d.ids) ? d.ids.map(String) : [];
      const hit = ids.filter((id) => guidanceById.has(id));
      for (const id of hit) guidanceById.get(id).consumedTask = String(d.task_id || '');
      if (!hit.length) {
        msgs.push({ type: 'system', ts: e.ts, order, kind: 'plan', text: `已注入 ${d.count || ids.length} 条指引${d.task_id ? ' → ' + d.task_id : ''}` });
      }
    } else if (name === 'guidance_expired') {
      const ids = Array.isArray(d.ids) ? d.ids.map(String) : [];
      for (const id of ids) {
        const g = guidanceById.get(id);
        if (g && g.status === 'pending') g.status = 'expired';
      }
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'warn', text: `${d.count || ids.length} 条指引过期未吸收` });
    } else if (name === 'guidance_withdrawn') {
      const g = guidanceById.get(String(d.id || ''));
      if (g) { if (g.status !== 'consumed') g.status = 'withdrawn'; }
      else msgs.push({ type: 'system', ts: e.ts, order, kind: 'warn', text: '撤回一条未吸收的指引' });
    } else if (name === 'guidance_compacted') {
      msgs.push({ type: 'system', ts: e.ts, order, kind: 'plan', text: '指引历史已压缩为快照' });
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
  // 有些 run 没有 task_notes 事件，但 tasks.json 里结果自带 summary/notes。
  // 没有正文时用它兜底，避免对话里只剩「8m30s」一行时长。
  for (const tm of taskMsg.values()) {
    if (tm.items.some((it) => it.kind === 'note')) continue;
    const t = taskIndex[tm.taskId];
    const summary = t && t.result && (t.result.summary || t.result.notes);
    if (summary) tm.items.unshift({ kind: 'note', text: String(summary), ts: tm.ts });
  }
  msgs.sort((a, b) => a.order - b.order);
  // 指引气泡（A3）：GET 列表 / 乐观插入的正文按 ts 插进时间线；没有 ts（刚
  // 发出去还没对账）的排到末尾。气泡的 chip 用 status + consumedTask 表达。
  for (const g of guidanceItems) {
    if (!g || !g.id) continue;
    const m = {
      type: 'user', subtype: 'guidance', ts: g.ts || '',
      gid: String(g.id), text: String(g.text || ''),
      target: g.target || null, status: g.status || 'pending',
      consumedTask: g.consumedTask || '',
    };
    if (!m.ts) { msgs.push(m); continue; }
    let i = msgs.length;
    while (i > 0 && String(msgs[i - 1].ts || '') > m.ts) i -= 1;
    msgs.splice(i, 0, m);
  }
  return { msgs, taskIndex };
}

export { buildMessages };
