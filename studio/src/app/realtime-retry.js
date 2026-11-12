/**
 * SSE 断线重连策略（C-18）。
 *
 * 抽成独立模块有两个原因：realtime.js 已到模块体量上限（320 行）；退避算法
 * 是纯函数，放在这里能直接被 node --test 覆盖，不必启动 Electron。
 */

/** 最多重试次数：5 次合计约 31 秒，足够跨过后端重启窗口。 */
const SSE_MAX_RETRIES = 5;
const SSE_RETRY_BASE_MS = 1000;
const SSE_RETRY_MAX_MS = 30000;

/**
 * 第 ``attempt`` 次（从 0 起）重试前的等待毫秒数。
 * 序列：1s → 2s → 4s → 8s → 16s，任何一次都不超过 30s。
 */
function sseRetryDelay(attempt) {
  const n = Number.isFinite(attempt) && attempt > 0 ? Math.floor(attempt) : 0;
  return Math.min(SSE_RETRY_MAX_MS, SSE_RETRY_BASE_MS * 2 ** n);
}

/** 是否还应该继续重试（达到上限即放弃，交给文件监听兜底）。 */
function canRetry(attempt) {
  return attempt < SSE_MAX_RETRIES;
}

export { SSE_MAX_RETRIES, canRetry, sseRetryDelay };
