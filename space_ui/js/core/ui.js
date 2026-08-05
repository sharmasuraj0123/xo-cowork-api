/* Shared UI helpers. */

let toastT=null;
export function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg;t.classList.add('is-on');
  clearTimeout(toastT);
  toastT=setTimeout(()=>t.classList.remove('is-on'),1900);
}
