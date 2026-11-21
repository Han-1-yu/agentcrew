/**
 * 对话页的纯数据层：筛选 / 搜索 / 工具参数摘要 / 导出 Markdown。
 *
 * 全部纯函数、零 DOM，便于 node:test 直接覆盖。视图层只负责把这些结果画出来，
 * 避免「过滤规则一套、复制出来的文本又是另一套」这类漂移。
 */
import { roleMeta } from '../contracts/enums.js';

//: 顺序即工具栏 chips 顺序；key 与 messageInFilter 的分支一一对应
const CONV_FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'user', label: '目标' },
  { key: 'agent', label: 'Agent' },
  { key: 'system', label: '系统' },
  { key: 'tool', label: '工具' },
  { key: 'artifact', label: '产物' },
  { key: 'gate', label: '门禁' },
];

function itemText(it) {
  if (!it) return '';
  const parts = [it.text, it.tool, it.path, it.layer, it.verdict, ...(it.issues || [])];
  if (it.args != null) {
    try { parts.push(typeof it.args === 'string' ? it.args : JSON.stringify(it.args)); } catch { /* 循环引用忽略 */ }
  }
  return parts.filter(Boolean).join(' ');
}

/** 一条消息里所有可被搜索到的文本（角色中英文、任务标题、正文、工具入参、产物路径…）。 */
function messageText(m, task) {
  if (!m) return '';
  if (m.type === 'user') return m.goal || m.text || '';
  if (m.type === 'system') return m.text || '';
  const rm = roleMeta(m.role);
  const parts = [m.role, rm.full, m.taskId, task && task.title];
  for (const it of m.items || []) parts.push(itemText(it));
  return parts.filter(Boolean).join(' ');
}

function hasItem(m, kind) {
  return m && m.type === 'agent' && (m.items || []).some((it) => it.kind === kind);
}

/** 类型筛选：工具/产物/门禁是「消息内包含该类条目」，一条消息可命中多个分类。 */
function messageInFilter(m, filter) {
  if (!filter || filter === 'all') return true;
  if (filter === 'user' || filter === 'agent' || filter === 'system') return m.type === filter;
  if (filter === 'tool' || filter === 'artifact' || filter === 'gate') return hasItem(m, filter);
  return true;
}

function filterMessages(msgs, opts, taskIndex) {
  const { query = '', filter = 'all' } = opts || {};
  const q = String(query == null ? '' : query).trim().toLowerCase();
  const index = taskIndex || {};
  return (msgs || []).filter((m) => {
    if (!messageInFilter(m, filter)) return false;
    if (!q) return true;
    return messageText(m, index[m.taskId]).toLowerCase().includes(q);
  });
}

/** chips 上的计数（分类之间允许重叠，仅作提示，不代表互斥分桶）。 */
function filterCounts(msgs) {
  const out = {};
  for (const f of CONV_FILTERS) {
    out[f.key] = (msgs || []).filter((m) => messageInFilter(m, f.key)).length;
  }
  return out;
}

function oneLine(v, max) {
  const s = String(v == null ? '' : v).replace(/\s+/g, ' ').trim();
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

/**
 * 工具调用的单行摘要：优先挑「人一眼能认出这一步在干嘛」的参数
 * （命令 / 路径 / 查询 / URL），没有再退回 JSON 截断。
 */
function summarizeToolArgs(args, max = 64) {
  const limit = max > 4 ? max : 4;
  if (args == null || args === '') return '';
  if (typeof args === 'string') return oneLine(args, limit);
  if (typeof args !== 'object') return oneLine(args, limit);
  const priority = ['command', 'cmd', 'path', 'file_path', 'file', 'query', 'q', 'url', 'pattern', 'name', 'target'];
  for (const key of priority) {
    const v = args[key];
    if (typeof v === 'string' && v.trim()) return oneLine(v, limit);
  }
  try { return oneLine(JSON.stringify(args), limit); } catch { return ''; }
}

function gateLabel(layer) {
  return { static: '静态校验', llm: 'LLM 评审', human: '人工确认', supervisor: '主管复核' }[layer] || layer || '门禁';
}

function gateVerdict(verdict) {
  return verdict === 'approve' ? '通过' : (verdict === 'reject' ? '驳回' : String(verdict || '—'));
}

/** 复制单条 Agent 消息用的纯文本（不含 DOM 结构，方便贴进 issue / 文档）。 */
function agentMessageText(m, task) {
  const rm = roleMeta(m.role);
  const head = `【${rm.full} · ${m.taskId}${task && task.title ? ' · ' + task.title : ''}】`;
  const lines = [head];
  for (const it of m.items || []) {
    if (it.kind === 'note' && it.text) lines.push(String(it.text).trim());
    else if (it.kind === 'tool') {
      const arg = summarizeToolArgs(it.args, 120);
      const state = it.ok === false ? '失败' : (it.ok === true ? '成功' : '未知');
      lines.push(`- 工具 ${it.tool || '—'}（${state}${it.dur != null ? '，' + it.dur + 'ms' : ''}）${arg ? '：' + arg : ''}`);
    } else if (it.kind === 'gate') {
      lines.push(`- 门禁 ${gateLabel(it.layer)}：${gateVerdict(it.verdict)}`);
    } else if (it.kind === 'artifact') {
      lines.push(`- 产物 ${it.path || '—'}`);
    }
  }
  return lines.filter(Boolean).join('\n');
}

function fmtSize(n) {
  if (n == null) return '';
  const v = Number(n);
  if (!Number.isFinite(v)) return '';
  if (v < 1024) return `${v} B`;
  if (v < 1048576) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1048576).toFixed(2)} MB`;
}

/**
 * 导出整段对话为 Markdown。`now` 可注入，单测不依赖当前时间。
 * 只导出可读摘要（正文 / 工具 / 门禁 / 产物），不塞原始 JSON。
 */
function buildTranscript(msgs, taskIndex, run, now) {
  const r = run || {};
  const index = taskIndex || {};
  const when = new Date(now == null ? Date.now() : now).toISOString().replace('T', ' ').slice(0, 19);
  const out = [`# ${r.project_goal || '运行对话'}`, ''];
  out.push(`> 运行 \`${r.run_id || r.dir || '—'}\` · 状态 ${r.status || '—'}${r.model ? ' · 模型 ' + r.model : ''} · 导出 ${when}`);
  out.push('');

  for (const m of msgs || []) {
    if (m.type === 'user') {
      out.push('## 用户目标', '', String(m.goal || '（未命名目标）').trim(), '');
    } else if (m.type === 'system') {
      out.push(`> ${m.text || ''}`, '');
    } else {
      const rm = roleMeta(m.role);
      const task = index[m.taskId];
      out.push(`## ${rm.full} · ${m.taskId}${task && task.title ? ' ' + task.title : ''}`, '');
      const notes = (m.items || []).filter((it) => it.kind === 'note' && it.text).map((it) => String(it.text).trim());
      if (notes.length) out.push(notes.join('\n\n'), '');
      const bullets = [];
      for (const it of m.items || []) {
        if (it.kind === 'tool') {
          const arg = summarizeToolArgs(it.args, 120);
          const state = it.ok === false ? '失败' : (it.ok === true ? '成功' : '未知');
          bullets.push(`- 工具 \`${it.tool || '—'}\`（${state}${it.dur != null ? '，' + it.dur + 'ms' : ''}）${arg ? '：' + arg : ''}`);
        } else if (it.kind === 'gate') {
          bullets.push(`- 门禁 ${gateLabel(it.layer)}：${gateVerdict(it.verdict)}${it.score != null ? '（' + it.score + ' 分）' : ''}`);
        } else if (it.kind === 'artifact') {
          bullets.push(`- 产物 \`${it.path || '—'}\`${it.size != null ? '（' + fmtSize(it.size) + '）' : ''}`);
        }
      }
      if (bullets.length) out.push(bullets.join('\n'), '');
    }
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
}

function transcriptFilename(run) {
  const id = String((run && (run.run_id || run.dir)) || 'run').split(/[\\/]/).filter(Boolean).pop() || 'run';
  return `agentcrew-${id.replace(/[^\w.-]+/g, '-')}-conversation.md`;
}

export {
  CONV_FILTERS,
  agentMessageText,
  buildTranscript,
  filterCounts,
  filterMessages,
  gateLabel,
  gateVerdict,
  hasItem,
  messageInFilter,
  messageText,
  summarizeToolArgs,
  transcriptFilename,
};
