/**
 * R40 流式正文面板：消费 SSE 的 `agent_stream_delta` 命名事件。
 *
 * 后端刻意不把正文写进 ledger（只记 delta_length），所以这块只能靠 SSE 实时
 * 累加，刷新页面后不回溯。正文是模型输出，属于不可信文本：一律用文本节点
 * 写入，绝不拼 innerHTML。
 */
import { $, h } from '../../lib/dom.js';

const MAX_CHARS = 120000;
let lastTaskId = '';
let charCount = 0;

function panel() { return $('#stream-section'); }
function output() { return $('#stream-output'); }

function trim(out) {
  // 高频流可能跑几十万字符；从最旧的节点开始丢，避免 DOM 无限膨胀。
  while (charCount > MAX_CHARS && out.firstChild) {
    const node = out.firstChild;
    charCount -= (node.textContent || '').length;
    out.removeChild(node);
  }
}

function bindStreamPanel() {
  const clear = $('#stream-clear');
  if (clear && !clear.dataset.bound) {
    clear.dataset.bound = '1';
    clear.addEventListener('click', () => resetStreamPanel());
  }
}

function resetStreamPanel() {
  const out = output();
  if (out) out.replaceChildren();
  const sec = panel();
  if (sec) sec.classList.add('hidden');
  const live = $('#stream-live');
  if (live) live.textContent = '待接收';
  lastTaskId = '';
  charCount = 0;
}

function appendStreamDelta(data) {
  const out = output();
  const sec = panel();
  if (!out || !sec) return;
  let delta = String(data.delta == null ? '' : data.delta);
  if (!delta) return;
  // 单帧就超过上限时先截断：否则下面的 trim 会把刚追加的节点整个删掉，
  // 本次内容一点不剩。保留尾部（用户更关心最新输出）。
  const room = MAX_CHARS - 2000;
  if (delta.length > room) delta = delta.slice(-room);

  sec.classList.remove('hidden');
  const taskId = String(data.task_id || '');
  if (taskId && taskId !== lastTaskId) {
    lastTaskId = taskId;
    const label = `── ${taskId}${data.role ? ' · ' + String(data.role) : ''} ──`;
    const head = h('div', { class: 'stream-task-head' }, label);
    out.appendChild(head);
    charCount += label.length;
  }
  // 模型输出可能夹 \n，用 pre-wrap 呈现；纯文本节点天然转义 HTML。
  // 合并相邻文本节点：后端逐 token 推 delta，不合并的话 12 万字符会变成
  // 几万个 Text 节点，DOM 会被拖垮。
  const last = out.lastChild;
  if (last && last.nodeType === 3) last.appendData(delta);
  else out.appendChild(document.createTextNode(delta));
  charCount += delta.length;
  trim(out);
  out.scrollTop = out.scrollHeight;
  const live = $('#stream-live');
  if (live) live.textContent = '接收中';
}

export { appendStreamDelta, bindStreamPanel, resetStreamPanel };
