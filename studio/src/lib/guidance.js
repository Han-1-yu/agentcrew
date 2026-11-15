/**
 * C10 切片二：中途指引的纯数据逻辑。
 *
 * 这里是渲染进程里唯一「可单测」的部分：不碰 window / DOM / Electron，
 * 只做归一化、请求体构造和错误码翻译。REST 副作用留在 features/guidance。
 * 后端契约见 artifacts/contracts/openapi.yaml 的 /runs/{id}/guidance*。
 */

const STATUS_LABEL = {
  pending: '待吸收',
  consumed: '已吸收',
  withdrawn: '已撤回',
  expired: '已过期',
};

const GUIDANCE_HINT = {
  NO_API: '请先在「设置 → 连接」配置后端 API 地址与具备 runs:admin 的 Key。',
  TRANSPORT: '后端没响应或已超时，确认 python main.py serve 正在运行。',
  HTTP_401: 'API Key 无效或未配置，请到「设置 → 连接」检查。',
  HTTP_403: '当前 Key 缺 runs:admin 作用域，无法写入 / 撤回指引。',
  HTTP_400: '指引内容或目标/过期参数不合法（检查是否为空、过期时刻是否在未来）。',
  HTTP_404: '该指引不存在，或已经被吸收 / 撤回 / 过期。',
  HTTP_409: 'run 已结束（或仍在运行、不允许压缩历史），这条动作不适用。',
  HTTP_429: '待吸收指引已达上限，等引擎吸收或先撤回旧的指引。',
  HTTP_503: '后端未配置 output_dir 或写盘失败。',
};

/** run 目录名（去掉盘符与上级路径），与后端 _validated_run_id 同口径。 */
function currentRunId(run, watchDir) {
  const raw = (run && (run.run_id || run.dir)) || watchDir || '';
  return String(raw).replace(/\\/g, '/').split('/').filter(Boolean).pop() || '';
}

function isExpired(iso, now = Date.now()) {
  if (!iso) return false;
  const ts = Date.parse(iso);
  return Number.isFinite(ts) && ts <= now;
}

/** 兼容旧后端（切片一没有 status/target/expires_at/withdrawn_at 字段）。 */
function normalizeGuidanceItem(raw, now = Date.now()) {
  const item = raw && typeof raw === 'object' ? raw : {};
  let status = item.status;
  if (!STATUS_LABEL[status]) {
    if (item.withdrawn_at) status = 'withdrawn';
    else if (item.consumed_at) status = 'consumed';
    else if (isExpired(item.expires_at, now)) status = 'expired';
    else status = 'pending';
  }
  return {
    id: String(item.id || ''),
    ts: item.ts || '',
    author: item.author || '',
    text: String(item.text || ''),
    consumed_at: item.consumed_at || null,
    target: item.target || null,
    expires_at: item.expires_at || null,
    withdrawn_at: item.withdrawn_at || null,
    status,
    label: STATUS_LABEL[status] || status,
  };
}

function normalizeGuidance(payload, now = Date.now()) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const rawItems = Array.isArray(data.items) ? data.items : [];
  const items = rawItems.map(item => normalizeGuidanceItem(item, now)).filter(i => i.id);
  const pending = Number.isFinite(data.pending)
    ? data.pending
    : items.filter(i => i.status === 'pending').length;
  return { runId: String(data.run_id || ''), items, pending };
}

/** 构造 POST /runs/{id}/guidance 的请求体；不定向 / 不过期时字段一律省略。 */
function buildGuidanceBody({ text, target, expiresMinutes, author = 'studio' } = {}) {
  const body = { text: String(text || '').trim(), author: String(author || 'studio') };
  if (target) body.target = String(target);
  const minutes = Number(expiresMinutes);
  if (Number.isFinite(minutes) && minutes > 0) {
    body.expires_in_seconds = Math.round(minutes * 60);
  }
  return body;
}

/** 任务下拉选项：永远带一个「不定向」选项，任务标题只做展示。 */
function guidanceTaskOptions(tasks) {
  const options = [{ value: '', label: '下一个派发的任务（不定向）' }];
  for (const task of (Array.isArray(tasks) ? tasks : [])) {
    if (!task || !task.id) continue;
    options.push({
      value: String(task.id),
      label: `${task.id}${task.title ? ` · ${String(task.title).slice(0, 24)}` : ''}`,
    });
  }
  return options;
}

function canWithdraw(item) {
  return !!item && item.status === 'pending';
}

function explainGuidanceError(err) {
  const code = (err && err.code) || '';
  if (GUIDANCE_HINT[code]) return GUIDANCE_HINT[code];
  const detail = (err && (err.detail || err.message)) || '';
  return String(detail || '未知错误，请查看后端日志。');
}

/** 显示用：定向 / 过期信息合成一句短描述。 */
function guidanceMeta(item) {
  const bits = [];
  if (item.target) bits.push(`定向 ${item.target}`);
  if (item.expires_at) bits.push(`过期 ${item.expires_at}`);
  if (item.author) bits.push(`by ${item.author}`);
  return bits.join(' · ');
}

export {
  STATUS_LABEL,
  buildGuidanceBody,
  canWithdraw,
  currentRunId,
  explainGuidanceError,
  guidanceMeta,
  guidanceTaskOptions,
  isExpired,
  normalizeGuidance,
  normalizeGuidanceItem,
};
