/**
 * 枚举元数据：任务状态 / 运行状态 / 角色 / 事件类型。
 * 颜色一律引用 CSS 变量（styles/tokens.css 提供 light/dark 双套），新增状态只改这里 + tokens。
 */
// TaskStatus 10 态（权威：task/state_machine.py）
// 颜色全部引用 CSS 变量，随 [data-theme] 切换；styles.css 提供 light/dark 双套 --status-*
const STATUS_META = {
  pending:   { label: '待启动', color: 'var(--status-pending)',    fill: 'var(--status-pending-bg)' },
  ready:     { label: '就绪',   color: 'var(--status-ready)',      fill: 'var(--status-ready-bg)' },
  running:   { label: '运行中', color: 'var(--status-running)',    fill: 'var(--status-running-bg)' },
  review:    { label: '门禁评审', color: 'var(--status-review)',   fill: 'var(--status-review-bg)' },
  rework:    { label: '返工中', color: 'var(--status-rework)',     fill: 'var(--status-rework-bg)' },
  blocked:   { label: '阻塞',   color: 'var(--status-blocked)',    fill: 'var(--status-blocked-bg)' },
  succeeded: { label: '已通过', color: 'var(--status-succeeded)',  fill: 'var(--status-succeeded-bg)' },
  failed:    { label: '失败',   color: 'var(--status-failed)',     fill: 'var(--status-failed-bg)' },
  skipped:   { label: '已跳过', color: 'var(--status-skipped)',    fill: 'var(--status-skipped-bg)' },
  cancelled: { label: '已取消', color: 'var(--status-cancelled)',  fill: 'var(--status-cancelled-bg)' },
  unknown:   { label: '未知',   color: 'var(--text-ter)',         fill: 'var(--bg-subtle)' },
};
const STATUS_ORDER = ['pending','ready','running','review','rework','blocked','succeeded','failed','skipped','cancelled'];

// RunStatus（实际观测值）：preparing/planning/awaiting_approval/running/paused/failed/completed/cancelled
const RUN_META = {
  preparing:        { label: '准备中',       dot: 'idle',    chip: 'idle' },
  planning:         { label: '规划中',       dot: 'running', chip: 'review' },
  awaiting_approval:{ label: '等待人工确认', dot: 'warning', chip: 'review' },
  running:          { label: '运行中',       dot: 'running', chip: 'running' },
  paused:           { label: '已暂停',       dot: 'paused',  chip: 'paused' },
  failed:           { label: '失败',         dot: 'error',   chip: 'failed' },
  completed:        { label: '已完成',       dot: 'done',    chip: 'done' },
  cancelled:        { label: '已取消',       dot: 'idle',    chip: 'idle' },
};

// RunPhase：planning / execution / delivery（代码路径，UI 派生）
const PHASES = ['规划', '执行', '交付'];

// 8 个角色（role 字段权威值）；颜色引用语义装饰 token（--purple/--pink/--sky/--indigo…）
const ROLE_META = {
  project_manager:  { label: 'PM',       full: '项目经理',   color: 'var(--purple)' },
  product_manager:  { label: '产品',     full: '产品经理',   color: 'var(--pink)' },
  architect:        { label: '架构',     full: '架构师',     color: 'var(--accent)' },
  backend_dev:      { label: '后端',     full: '后端开发',   color: 'var(--success)' },
  frontend_dev:     { label: '前端',     full: '前端开发',   color: 'var(--sky)' },
  qa_engineer:      { label: '测试',     full: '测试工程师', color: 'var(--orange)' },
  devops_engineer:  { label: '运维',     full: '运维工程师', color: 'var(--indigo)' },
  technical_writer: { label: '文档',     full: '文档工程师', color: 'var(--text-ter)' },
};
const roleMeta = (id) => ROLE_META[id] || { label: id || '?', full: id || '未知角色', color: 'var(--text-ter)' };

// Agent 运行时状态（由任务派生：queued / executing / reviewing / done）
const AGENT_STATUS_LABEL = {
  queued: '等待派发',
  executing: '执行中',
  reviewing: '评审中',
  done: '空闲',
};
/** 把 agent 状态翻译成界面文案；未知状态原样返回，避免吞掉新值。 */
function agentStatusLabel(st) { return AGENT_STATUS_LABEL[st] || st || '—'; }

// ledger 事件类型（event 字段权威值，见 DATA-CONTRACT-REFERENCE §3）
// 颜色引用语义 token，随主题切换；淡填充由 CSS color-mix 派生（不再 JS 拼 hex alpha）
const EVENT_META = {
  run_started:      { label: '运行创建',   color: 'var(--accent)' },
  llm_call:         { label: 'LLM 调用',   color: 'var(--sky)' },
  plan_created:     { label: '任务生成',   color: 'var(--success)' },
  plan_scored:      { label: '计划评分',   color: 'var(--sky)' },
  task_started:     { label: '任务开始',   color: 'var(--orange)' },
  task_notes:       { label: '任务笔记',   color: 'var(--text-ter)' },
  artifact_written: { label: '产物生成',   color: 'var(--success)' },
  task_finished:    { label: '任务完成',   color: 'var(--success)' },
  task_transition:  { label: '状态跃迁',   color: 'var(--text-ter)' },
  gate_result:      { label: '门禁结果',   color: 'var(--purple)' },
  run_finished:     { label: '运行结束',   color: 'var(--text-ter)' },
  llm_failover:     { label: '故障转移',   color: 'var(--warn)' },
  llm_model_switched:{ label: '模型切换',  color: 'var(--sky)' },
  budget_warning:   { label: '预算预警',   color: 'var(--warn)' },
  budget_exhausted: { label: '预算耗尽',   color: 'var(--error)' },
  task_timeout:     { label: '任务超时',   color: 'var(--warn)' },
  checkpoint_saved: { label: '检查点保存', color: 'var(--text-ter)' },
  human_approved:   { label: '人工批准',   color: 'var(--success)' },
  run_cancelled:    { label: '运行取消',   color: 'var(--error)' },
  error:            { label: '错误',       color: 'var(--error)' },
};
const EVENT_TYPES = Object.keys(EVENT_META);

/* ============================================================
   全局状态
   ============================================================ */

const ROLE_ORDER = ['project_manager','product_manager','architect','backend_dev','frontend_dev','qa_engineer','devops_engineer','technical_writer'];

/* ============================================================
   内置供应商（对齐 agentcrew/models/config.py）
   引擎按 "<provider>:<model>" 解析模型别名
   ============================================================ */
/* ============================================================
   内置供应商（对齐 agentcrew/models/config.py 的 PROVIDER_PRESETS，共 27 个）
   引擎按 "<provider>:<model>" 解析模型别名
   字段：category / family / search_aliases / endpoint_candidates / api_key_url
   ============================================================ */
const PROV_CATEGORY_LABELS = {
  aggregator: '聚合', national: '国内', international: '国际',
  local: '本地', specialized: '专用',
};
const RUN_STATUS_COLOR = {
  running: 'var(--accent)', completed: 'var(--success)', failed: 'var(--error)',
  paused: 'var(--warn)', cancelled: 'var(--text-ter)', planning: 'var(--sky)',
  preparing: 'var(--text-ter)', awaiting_approval: 'var(--purple)',
};

export {
  AGENT_STATUS_LABEL,
  agentStatusLabel,
  EVENT_META,
  EVENT_TYPES,
  PHASES,
  PROV_CATEGORY_LABELS,
  ROLE_META,
  ROLE_ORDER,
  RUN_META,
  RUN_STATUS_COLOR,
  STATUS_META,
  STATUS_ORDER,
  roleMeta,
};
