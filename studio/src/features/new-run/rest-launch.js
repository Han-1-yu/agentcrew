/**
 * C1：REST 创建通道（POST /runs）。
 *
 * 已连 API（apiBaseUrl + Key 已配）时优先走 REST：后端直接返回 run_id，
 * 不再依赖目录扫描竞态定位。失败处理纪律（方案 §3.2）：
 *  - 401 / 403 / 503 / 400 / 未配 API → 调用方显式降级 CLI；
 *  - 超时 / 传输错误 → **绝不自动降级**（run 可能已在服务端创建，双发
 *    比失败更糟），只报错并把 run_id 未知这件事说清楚。
 */
import { apiFetch } from '../../lib/api.js';
import { S } from '../../lib/store.js';

/** REST 通道可用：地址已配 + Key 已配（与审批/指引页同一判定）。 */
function restCapable() {
  return !!(S.settings && S.settings.apiBaseUrl) && !!S.apiKeyConfigured;
}

/**
 * 模式 / 参数 → options 白名单映射（api/run_options.py）。
 * ask（--plan-only）在白名单里没有等价键 → 调用方须走 CLI 通道。
 * plan 模式：approval_mode=deferred 把「问人」外包给审批单（服务端会把
 * gate 提为 both：计划 + 交付两个卡点，比 CLI 的 plan 稍宽）。
 */
function buildRunOptions({ mode, mock, model, maxCost, maxLlmCalls, flowName } = {}) {
  const options = {};
  if (mock) options.mock = true;
  if (model) options.default_model = String(model);
  if (Number(maxCost) > 0) options.max_cost_usd = Number(maxCost);
  if (Number(maxLlmCalls) > 0) options.max_llm_calls = Math.round(Number(maxLlmCalls));
  if (mode === 'plan') options.approval_mode = 'deferred';
  if (flowName) options.flow_name = String(flowName);
  return options;
}

/**
 * POST /runs；成功返回 { run_id, status }。
 * 超时上限 40s：后端要等 run_started（自身 30s 上限）才回 202。
 */
async function createRunViaRest({ goal, mode, mock, model, maxCost, maxLlmCalls, images } = {}) {
  const body = {
    goal: String(goal || '').trim(),
    options: buildRunOptions({ mode, mock, model, maxCost, maxLlmCalls }),
  };
  if (Array.isArray(images) && images.length) body.images = images.slice(0, 4);
  return apiFetch('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    timeoutMs: 40000,
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * REST 创建成功后按 run_id 定位本地目录并切换。
 * 找不到（server 的 output_dir 与本地不一致 / 队列模式落盘延迟）返回 null，
 * 由调用方提示用户手动切换，不假装切换成功。
 */
async function locateAndSwitchRestRun(runId, { timeoutMs = 15000, switchRun } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const runs = (await window.studio.listRuns()) || [];
      const hit = runs.find((r) => r.runId === runId || String(r.dir || '').replace(/\\/g, '/').endsWith('/' + runId));
      if (hit) {
        await switchRun(hit.dir);
        return hit.dir;
      }
    } catch { /* 列表抖动：下一轮再试 */ }
    if (Date.now() >= deadline) return null;
    await sleep(800);
  }
}

/**
 * 传输类错误（超时 / 断连 / 中断）：请求结果未知——run 可能已在服务端创建，
 * 此时降级 CLI 等于双发。调用方只能报错，不能自动重试或降级。
 * 有 HTTP 状态码的错误（401/403/429/503…）是服务端的明确答复，可以降级。
 */
function isTransportError(err) {
  const code = (err && err.code) || '';
  return code === 'TRANSPORT' || code === '';
}

export { buildRunOptions, createRunViaRest, isTransportError, locateAndSwitchRestRun, restCapable };
