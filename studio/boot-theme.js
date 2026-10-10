'use strict';
// 首帧前同步预置主题，避免 applyTheme() 在异步 getSettings 后才执行导致的浅色闪烁(FOUC)。
// 渲染端在 applyTheme() 时把已解析的实际主题缓存到 localStorage；默认 dark（与 main.js defaultSettings 一致）。
// 单独成文件而非内联 <script>：当前 CSP script-src 回退 default-src 'self'，不允许内联脚本。
try {
  var __t = localStorage.getItem('agentcrew:theme');
  document.documentElement.dataset.theme = (__t === 'light' || __t === 'dark') ? __t : 'dark';
} catch (e) {
  document.documentElement.dataset.theme = 'dark';
}
