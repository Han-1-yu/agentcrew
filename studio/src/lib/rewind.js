/**
 * R62：Studio 的 rewind（回滚到任意任务并重跑）纯数据逻辑。
 *
 * 后端 POST /runs/{id}/rewind 会重置目标任务与其下游、快照旧产物并把 run 置为
 * interrupted；调用方随后必须 POST /runs/{id}/resume 才会真正重跑。dry_run 只算
 * 影响集、不写盘，UI 先预览再动手。
 */

const REWIND_HINT = {
  NO_API: '请先在「设置 → 连接」配置后端 API 地址与具备 runs:write 的 Key。',
  TRANSPORT: '后端没响应或已超时，确认 python main.py serve 正在运行。',
  HTTP_401: 'API Key 无效或未配置，请到「设置 → 连接」检查。',
  HTTP_403: '当前 Key 缺 runs:write 作用域，无法回滚。',
  HTTP_400: '任务 id 不存在或参数不合法；请从下拉里选一个板上任务。',
  HTTP_409: 'run 正在运行中，请先取消（cancel）再回滚；或存在 resume 锁冲突。',
  HTTP_503: '后端未配置 settings，无法回滚。',
};

function buildRewindBody({ task, instruction = '', dryRun = false } = {}) {
  return {
    task: String(task || '').trim(),
    instruction: String(instruction || ''),
    dry_run: !!dryRun,
  };
}

/** 兼容后端 RewindResult.as_dict()；缺字段一律给安全的默认值。 */
function normalizeRewindResult(payload) {
  const data = payload && typeof payload === 'object' ? payload : {};
  return {
    runId: String(data.run_id || ''),
    target: String(data.target || ''),
    affected: Array.isArray(data.affected) ? data.affected.map(String) : [],
    snapshotDir: data.snapshot_dir || null,
    discardedArtifacts: Number.isFinite(data.discarded_artifacts)
      ? data.discarded_artifacts : 0,
    statusBefore: String(data.status_before || ''),
    statusAfter: String(data.status_after || ''),
    instruction: String(data.instruction || ''),
    dryRun: !!data.dry_run,
  };
}

/** 预览区的一句人话：影响几个任务 / 删几个产物 / 是否只预览。 */
function rewindImpactText(result) {
  const r = normalizeRewindResult(result);
  const prefix = r.dryRun ? '预览（不写盘）' : '已回滚';
  const snapshot = r.snapshotDir ? `；旧产物快照 ${r.snapshotDir}` : '';
  return `${prefix}：从 ${r.target || '?'} 起影响 ${r.affected.length} 个任务`
    + `（${r.affected.join('、') || '无'}），移除 ${r.discardedArtifacts} 个产物`
    + `${snapshot}。${r.dryRun ? '' : '回滚后需 resume 才会重跑。'}`;
}

/** 已回滚（非 dry-run）后才需要 resume。 */
function needsResume(result) {
  const r = normalizeRewindResult(result);
  return !r.dryRun && r.affected.length > 0;
}

function explainRewindError(err) {
  const code = (err && err.code) || '';
  if (REWIND_HINT[code]) return REWIND_HINT[code];
  const detail = (err && (err.detail || err.message)) || '';
  return String(detail || '未知错误，请查看后端日志。');
}

/** 回滚弹窗的任务下拉：回退到「至少有一个占位项」，避免出现空 select。 */
function rewindTaskOptions(tasks, preferred) {
  const list = (Array.isArray(tasks) ? tasks : [])
    .filter(task => task && task.id)
    .map(task => ({
      value: String(task.id),
      label: `${task.id}${task.title ? ` · ${String(task.title).slice(0, 24)}` : ''}`,
    }));
  if (preferred && !list.some(o => o.value === String(preferred))) {
    list.unshift({ value: String(preferred), label: String(preferred) });
  }
  return list;
}

export {
  buildRewindBody,
  explainRewindError,
  needsResume,
  normalizeRewindResult,
  rewindImpactText,
  rewindTaskOptions,
};
