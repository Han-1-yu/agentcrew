/**
 * 模态框原语：showModal(节点) / closeModal() / confirmAction(二次确认)。
 */
import { $, h } from '../lib/dom.js';

// C-4：closeModal 的 200ms 隐藏定时器必须可取消。此前 closeModal 紧跟
// showModal（如「取消 → 立刻打开另一个弹窗」）时，旧定时器会在 200ms 后把
// 新弹窗一起 hidden 掉，表现为「弹窗闪一下就没了」。
let hideTimer = null;

function showModal(node){
  if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  const m=$('#modal'); m.innerHTML=''; m.appendChild(node); $('#modal-mask').classList.remove('hidden'); m.classList.remove('hidden'); requestAnimationFrame(()=>{ m.classList.add('show'); $('#modal-mask').classList.add('show'); }); }
function closeModal(){
  const m=$('#modal'); m.classList.remove('show'); $('#modal-mask').classList.remove('show');
  if (hideTimer) clearTimeout(hideTimer);
  hideTimer = setTimeout(()=>{ m.classList.add('hidden'); $('#modal-mask').classList.add('hidden'); hideTimer = null; },200);
}
// 是否有模态/详情面板处于打开：用于屏蔽底层导航快捷键，避免弹窗背后切换页面
function anyOverlayOpen(){
  const m=document.getElementById('modal');
  const d=document.getElementById('detail-panel');
  return (!!m && !m.classList.contains('hidden')) || (!!d && !d.classList.contains('hidden'));
}
$('#modal-mask').addEventListener('click', closeModal);
function confirmAction(title, message, onOk, okLabel) {
  showModal(h('div',{},
    h('h3',{class:'modal-title'},title),
    h('div',{class:'confirm-text'},message),
    h('div',{class:'modal-actions'},
      h('button',{class:'btn',onclick:closeModal},'取消'),
      h('button',{class:'btn btn-danger',onclick:()=>{closeModal();onOk();}},okLabel||'确认'))));
}

/* ============================================================
   Mock 数据（对齐 reference-run 真实结构）
   ============================================================ */

export { anyOverlayOpen, closeModal, confirmAction, showModal };
