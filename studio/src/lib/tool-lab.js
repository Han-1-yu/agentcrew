/**
 * Q2 工具试跑页的纯函数：入参模板、开关口径、结果格式化。
 *
 * 放在 lib 是为了能 `node --test` 直测（不碰 window / DOM）；页面只负责把
 * 这些结果画出来。口径与后端 `agentcrew/tools/lab.py::{describe_tools,run_trial}`
 * 对齐：`kind` 三类、200 的 `ok=false` 是「工具报错」而不是请求失败。
 */

const KIND_LABELS = { file: '文件', network: '网络', sandbox: '沙箱' };

/** 工具类别的中文标签；未知类别原样返回，避免把新类别藏掉。 */
function kindLabel(kind) {
  return KIND_LABELS[kind] || String(kind || '其他');
}

/** 按 schema 类型给一个占位值；只用于模板，不参与校验。 */
function sampleValue(schema = {}) {
  const type = schema.type;
  if (type === 'integer' || type === 'number') return 0;
  if (type === 'boolean') return false;
  if (type === 'array') return [];
  if (type === 'object') return {};
  return '';
}

/**
 * 入参模板：只预填**必填**字段。
 *
 * 可选项留空是有意的——模板里塞满空串会让「试跑」看起来像已经填好了，用户
 * 不删就可能把无意义的空参数发给工具（后端 schema 校验会拦，但那是多一步）。
 */
function argsTemplate(tool = {}) {
  const schema = tool.input_schema || {};
  const props = schema.properties || {};
  const required = schema.required || [];
  const out = {};
  for (const [name, prop] of Object.entries(props)) {
    if (required.includes(name)) out[name] = sampleValue(prop);
  }
  return out;
}

/** 解析用户输入的 JSON 文本；返回 `{ok,args}` 或 `{ok:false,error}`。 */
function parseArgs(text) {
  const raw = String(text == null ? '' : text).trim();
  if (!raw) return { ok: true, args: {} };
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    return { ok: false, error: `JSON 解析失败：${err.message}` };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: '入参必须是 JSON 对象（形如 {"path": "workspace/a.txt"}）' };
  }
  return { ok: true, args: parsed };
}

/** 耗时展示：1 秒以下给毫秒，以上给秒（与 run 报告的读法一致）。 */
function formatDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return '—';
  return n < 1000 ? `${n.toFixed(1)} ms` : `${(n / 1000).toFixed(2)} s`;
}

/**
 * 顶部状态 chips：把 `GET /tools` 的四个开关口径摊平。
 *
 * 每一条都对应「清单里为什么有 / 没有某个工具」，所以 `code_exec_enabled=false`
 * 时不能说「已关闭」就完事——要说明 `run_python` / `run_tests` 因此不在清单里，
 * 否则用户会以为页面的清单是漏了。
 */
function statusChips(payload = {}) {
  const chips = [];
  chips.push(payload.tools_enabled
    ? { label: '工具执行已开启', tone: 'ok' }
    : { label: '工具执行未开启 · 试跑不可用', tone: 'warn' });
  chips.push(payload.code_exec_enabled
    ? {
      label: `代码执行：${payload.sandbox_backend || 'local'}`,
      tone: payload.sandbox_backend === 'docker' ? 'ok' : 'warn',
    }
    : { label: '代码执行未开启 · 不列 run_python / run_tests', tone: 'mute' });
  chips.push(payload.search_provider
    ? { label: `搜索提供方：${payload.search_provider}`, tone: 'ok' }
    : { label: '未配置搜索 · 不列 web_search', tone: 'mute' });
  if (payload.trial_dir) {
    chips.push({ label: `沙箱目录：${payload.trial_dir}/`, tone: 'mute' });
  }
  return chips;
}

/** 试跑结果摘要：`ok=false` 是工具自身失败，文案要与请求错误区分开。 */
function resultSummary(result = {}) {
  if (!result) return { tone: 'mute', title: '无结果', detail: '' };
  if (result.ok) {
    return {
      tone: 'ok',
      title: `试跑通过 · ${formatDuration(result.duration_ms)}`,
      detail: result.sandbox ? `沙箱：${result.sandbox}` : '',
    };
  }
  return {
    tone: 'err',
    title: `试跑失败 · ${formatDuration(result.duration_ms)}`,
    detail: String(result.error || '工具返回 ok=false，但未带错误信息'),
  };
}

export {
  argsTemplate,
  formatDuration,
  kindLabel,
  parseArgs,
  resultSummary,
  statusChips,
};
