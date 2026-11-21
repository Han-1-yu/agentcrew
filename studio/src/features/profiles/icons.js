/**
 * 档案页专用线性图标：24×24、1.6 描边、currentColor。
 * 只在本页使用，不塞进全局 icons.js（那里只有播放/暂停这类运行控制）。
 */
import { s } from '../../lib/dom.js';

const PATHS = {
  plus: ['M12 5v14', 'M5 12h14'],
  refresh: ['M20 11a8 8 0 1 0-2.34 5.66', 'M20 4v7h-7'],
  search: ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z', 'M20 20l-3.6-3.6'],
  edit: ['M4 20h4L19 9l-4-4L4 16v4z', 'M13.5 6.5l4 4'],
  copy: ['M9 9h11v11H9z', 'M5 15V5a2 2 0 0 1 2-2h10'],
  download: ['M12 3v12', 'M7 10l5 5 5-5', 'M4 21h16'],
  trash: ['M4 7h16', 'M9 7V4h6v3', 'M6 7l1 13h10l1-13'],
  warn: ['M12 3l9 17H3L12 3z', 'M12 9v5', 'M12 17h.01'],
  info: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'M12 11v6', 'M12 7h.01'],
  folder: ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z'],
  check: ['M4 12l5 5L20 6'],
  upload: ['M12 21V9', 'M7 14l5-5 5 5', 'M4 3h16'],
};

function icon(name, size = 16) {
  return s('svg', {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', 'stroke-width': 1.6,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true',
  }, ...(PATHS[name] || []).map((d) => s('path', { d })));
}

export { icon };
