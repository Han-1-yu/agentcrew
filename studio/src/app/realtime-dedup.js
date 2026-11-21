/**
 * 事件去重键（从 realtime.js 拆出，纯计算 + 只读 S.dash）。
 *
 * 两层键：
 * 1. `eventKey`——SSE/文件监听同一来源内的逐字重复（文件监听会整份回放）；
 * 2. `semanticKey`——跨通道语义键：SSE 帧的 data 是裸业务 dict、ts 是渲染端
 *    本地时间，ledger 行是 {ts,event,data} 包装，原键在两种来源间必然失配。
 *    用「事件语义 + 3 秒窗口」识别同一事件，避免双通道/重连时条目翻倍。
 */
import { S } from '../lib/store.js';

function eventKey(e) {
  return `${e.ts || ''}|${e.event || ''}|${JSON.stringify(e.data || {})}`;
}

/* 窗口只看最近 12 条。 */
function semanticKey(e) {
  const d = e.data || {};
  return [e.event || '', d.task_id || d.taskId || '', d.to_status || d.to || d.status || '',
    d.from_status || d.from || '', d.checkpoint || '', d.id || d.approval_id || '',
    d.reason || ''].join('|');
}

/** 同一语义事件是否已出现在最近 3 秒内（只看最后 12 条，避免 O(n) 扫描）。 */
function nearDuplicate(e) {
  const list = (S.dash && S.dash.events) || [];
  const key = semanticKey(e);
  const t = Date.parse(e.ts || '') || 0;
  for (let i = list.length - 1, n = 0; i >= 0 && n < 12; i--, n++) {
    const prev = list[i];
    if (semanticKey(prev) !== key) continue;
    if (Math.abs(t - (Date.parse(prev.ts || '') || 0)) <= 3000) return true;
  }
  return false;
}

export { eventKey, nearDuplicate, semanticKey };
