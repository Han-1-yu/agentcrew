/**
 * 连接状态指示器（顶栏右侧的「SSE 实时 / 文件监听 / 演示模式」）。
 *
 * 从 realtime.js 抽出来：那里已到模块体量上限，而这个函数只依赖 $ 与 S，
 * 放 ui 层不引入任何新依赖方向。
 */
import { $ } from '../lib/dom.js';
import { S } from '../lib/store.js';

function updateConnIndicator() {
  const el = $('#conn-indicator');
  if (!el) return;
  if (S.sseConnected) {
    el.textContent = 'SSE 实时';
    el.className = 'conn-indicator live';
  } else if (S.realMode && S.watchDir) {
    el.textContent = '文件监听';
    el.className = 'conn-indicator watch';
  } else {
    el.textContent = '演示模式';
    el.className = 'conn-indicator demo';
  }
}

export { updateConnIndicator };
