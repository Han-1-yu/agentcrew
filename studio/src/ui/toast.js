/**
 * 轻提示：右下角短反馈，自动消失。
 */
import { $, h } from '../lib/dom.js';

function toast(type, title, msg, ms) {
  const box = $('#toasts');
  const t = h('div',{class:`toast ${type||''}`}, h('div',{class:'toast-title'},title), msg?h('div',{class:'toast-msg'},msg):null);
  box.appendChild(t);
  setTimeout(()=>{ t.style.opacity='0'; t.style.transition='opacity .25s'; setTimeout(()=>t.remove(),300); }, ms||(type==='error'?8000:4000));
}

export { toast };
