/**
 * 本机 HTTP API 客户端（后端 `main.py serve` 暴露的 REST 接口）。
 * 注意：Electron 主进程的 IPC 走 preload 暴露的 window.studio，不在本文件内。
 */
import { S } from './store.js';

/* 后端 API 调用：无 apiBaseUrl 直接 reject；8s 超时；失败由调用方静默降级（不弹 toast） */
async function apiFetch(path, options = {}) {
  const base = S.settings && S.settings.apiBaseUrl ? String(S.settings.apiBaseUrl).replace(/\/+$/, '') : '';
  if (!base) { const e = new Error('未配置后端 API 地址'); e.code = 'NO_API'; throw e; }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), options.timeoutMs || 8000);
  try {
    const res = await fetch(base + path, { ...options, signal: ctrl.signal });
    if (!res.ok) {
      const e = new Error('HTTP ' + res.status);
      e.code = 'HTTP_' + res.status;
      // 后端把可读原因放在 {"error": "..."}：带上它，调用方才能说人话
      try { const j = await res.json(); if (j && j.error) e.detail = String(j.error); } catch {}
      throw e;
    }
    return await res.json();
  } finally { clearTimeout(timer); }
}

export { apiFetch };
