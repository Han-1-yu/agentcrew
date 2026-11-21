/**
 * 复制到剪贴板：优先异步 Clipboard API，失败退回隐藏 textarea + execCommand。
 *
 * 为什么保留两条路：Electron 的 file:// 页面在部分窗口焦点状态下拿不到
 * clipboard 权限；没有兜底时「复制」按钮会静默失败，用户以为已经复制了。
 * 返回布尔值让调用方决定提示成功还是失败，不在这里弹 toast。
 */
async function copyText(text) {
  const value = String(text == null ? '' : text);
  if (!value) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch { /* 落到 execCommand */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

export { copyText };
