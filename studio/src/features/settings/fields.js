/**
 * 设置页表单读写：把散落在各组的输入框统一成一次采集。
 * 新增字段时改这里 + 对应组的 builder，保存逻辑不用动。
 */
import { $ } from '../../lib/dom.js';

/** 从 DOM 读取当前表单值，组装成可写回主进程的设置对象。 */
function collectSettings(cfg) {
  return {
    pythonCmd: $('#set-python').value.trim() || 'python',
    outputDir: $('#set-output').value.trim(),
    defaultModel: $('#set-model-custom').value.trim() || $('#set-model').value.trim(),
    maxCostUsd: Math.max(0, parseFloat($('#set-maxcost').value) || 0),
    apiBaseUrl: $('#set-api').value.trim(),
    // V7 网关高级功能
    redactEnabled: $('#set-redact-enabled').checked,
    redactApiKeys: $('#set-redact-apikeys').checked,
    redactPii: $('#set-redact-pii').checked,
    redactCredentials: $('#set-redact-creds').checked,
    redactCustomWords: $('#set-redact-custom').value.trim(),
    routingStrategy: cfg.routingStrategy || 'weighted',
    affinityMode: cfg.affinityMode || 'off',
    affinityTtlDays: Math.max(1, parseInt($('#set-affinity-ttl').value) || 7),
  };
}

export { collectSettings };
