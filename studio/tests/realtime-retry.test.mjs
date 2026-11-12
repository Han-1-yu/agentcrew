import { test } from 'node:test';
import assert from 'node:assert';
import { SSE_MAX_RETRIES, canRetry, sseRetryDelay } from '../src/app/realtime-retry.js';

test('退避序列按 2 的幂增长，首次 1s', () => {
  assert.equal(sseRetryDelay(0), 1000);
  assert.equal(sseRetryDelay(1), 2000);
  assert.equal(sseRetryDelay(2), 4000);
  assert.equal(sseRetryDelay(3), 8000);
  assert.equal(sseRetryDelay(4), 16000);
});

test('退避上限 30s（防止无界等待）', () => {
  assert.equal(sseRetryDelay(5), 30000);
  assert.equal(sseRetryDelay(10), 30000);
});

test('非法入参回落首次延迟，不产生 NaN', () => {
  for (const bad of [NaN, Infinity, -1, 1.7, '2', null, undefined]) {
    const d = sseRetryDelay(bad);
    assert.ok(Number.isFinite(d) && d > 0, `delay(${String(bad)}) = ${d}`);
    assert.ok(d <= 30000);
  }
});

test('重试次数上限为 5', () => {
  assert.equal(canRetry(0), true);
  assert.equal(canRetry(4), true);
  assert.equal(canRetry(SSE_MAX_RETRIES), false);
  assert.equal(canRetry(99), false);
});
