/**
 * 后端 REST 客户端。
 *
 * 请求由主进程代理（window.studio.apiRequest → net.fetch），不再在渲染进程直接
 * fetch：后端 serve 没有 CORS 头，file:// 源的请求会被浏览器拦下；同时认证头
 * 由主进程统一注入，Key 不必进入渲染进程。
 *
 * 对外契约保持不变：成功返回解析后的 JSON；失败抛带 code / detail 的 Error，
 * 调用方的静默降级与 explain() 文案都不用改。
 */
import { S } from './store.js';

function noApi() {
  const e = new Error('未配置后端 API 地址');
  e.code = 'NO_API';
  return e;
}

async function apiFetch(path, options = {}) {
  if (!window.studio || !window.studio.apiRequest) throw noApi();
  // 实际地址由主进程 settings 决定，这里只判「有没有配」。
  if (!(S.settings && S.settings.apiBaseUrl)) throw noApi();

  // 只传可结构化克隆的字段；AbortSignal / 函数等留在渲染进程。
  const wire = {
    method: options.method || 'GET',
    headers: options.headers || {},
    timeoutMs: options.timeoutMs || 8000,
  };
  if (options.body != null) wire.body = options.body;

  let r;
  try {
    r = await window.studio.apiRequest(String(path), wire);
  } catch (e) {
    const err = new Error(String((e && e.message) || e));
    err.code = 'TRANSPORT';
    throw err;
  }
  if (!r || !r.ok) {
    const status = (r && r.status) || 0;
    const e = new Error('HTTP ' + status);
    // status=0 = 后端没起 / 超时 / 主进程侧没配地址。沿用 NO_API，
    // 让 explain() 给出「先启动 serve」的引导，而不是落到通用的 HTTP_0。
    e.code = status === 0 ? 'NO_API' : 'HTTP_' + status;
    e.detail = (r && r.error) || '';
    throw e;
  }
  return r.data;
}

export { apiFetch };
