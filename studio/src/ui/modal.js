/**
 * 模态框原语：showModal(节点) / closeModal() / confirmAction(二次确认)。
 */
import { $, h } from '../lib/dom.js';

function showModal(node){ const m=$('#modal'); m.innerHTML=''; m.appendChild(node); $('#modal-mask').classList.remove('hidden'); m.classList.remove('hidden'); requestAnimationFrame(()=>{ m.classList.add('show'); $('#modal-mask').classList.add('show'); }); }
function closeModal(){ const m=$('#modal'); m.classList.remove('show'); $('#modal-mask').classList.remove('show'); setTimeout(()=>{ m.classList.add('hidden'); $('#modal-mask').classList.add('hidden'); },200); }
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
