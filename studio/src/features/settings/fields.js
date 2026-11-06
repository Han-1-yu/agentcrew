/**
 * 设置页表单读写：把散落在各组的输入框统一成一次采集。
 * 新增字段时改这里 + 对应组的 builder，保存逻辑不用动。
 */
import { $ } from '../../lib/dom.js';

import { readNumber, readUrl, showFieldError } from '../../lib/validate.js';

/**
 * 从 DOM 读取当前表单值，组装成可写回主进程的设置对象。
 *
 * 返回 ``{ values, error }``：数值与 URL 统一走 lib/validate，非法时**不保存**
 * 并把错误回报给调用方（此前负数 / Infinity / 随便一个字符串都会直接落盘，
 * 之后所有 fetch 与 SSE 才开始莫名其妙地失败）。
 */
function collectSettings(cfg) {
  const maxCostEl = $('#set-maxcost');
  const ttlEl = $('#set-affinity-ttl');
  const apiEl = $('#set-api');
  const cost = readNumber(maxCostEl,
    { min: 0, max: 1000000, fallback: 0, label: '预算上限' });
  const ttl = readNumber(ttlEl,
    { min: 1, max: 30, integer: true, fallback: 7, label: '亲和 TTL' });
  const api = readUrl(apiEl, { label: '后端 API 地址' });
  showFieldError(maxCostEl, null, cost.error);
  showFieldError(ttlEl, null, ttl.error);
  showFieldError(apiEl, null, api.error);
  const values = {
    pythonCmd: $('#set-python').value.trim() || 'python',
    outputDir: $('#set-output').value.trim(),
    defaultModel: $('#set-model-custom').value.trim() || $('#set-model').value.trim(),
    maxCostUsd: cost.value,
    apiBaseUrl: api.value,
    // V7 网关高级功能
    redactEnabled: $('#set-redact-enabled').checked,
    redactApiKeys: $('#set-redact-apikeys').checked,
    redactPii: $('#set-redact-pii').checked,
    redactCredentials: $('#set-redact-creds').checked,
    redactCustomWords: $('#set-redact-custom').value.trim(),
    routingStrategy: cfg.routingStrategy || 'weighted',
    affinityMode: cfg.affinityMode || 'off',
    affinityTtlDays: ttl.value,
  };
  return { values, error: cost.error || ttl.error || api.error };
}

export { collectSettings };
