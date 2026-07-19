/* Shared UI helpers. */

let toastT=null;
export function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg;t.classList.add('is-on');
  clearTimeout(toastT);
  toastT=setTimeout(()=>t.classList.remove('is-on'),1900);
}

/* Shared byte formatter + bounded-tree renderer (payload shape from
   xo_overview.py's _build_tree). Used by the Overview view and the Files
   tab's List mode — tree CSS lives in overview.css (.ovtree et al). */
export const kb=n=>{n=Number(n)||0;return n>=1e6?(n/1e6).toFixed(1)+' MB':n>=1e3?(n/1e3).toFixed(1)+' KB':n+' B';};
const escT=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
export function treeHtml(node,depth){
  if(node.type==='file')
    return `<div class="tf"><span class="tfn">${escT(node.name)}</span><span class="tfs">${node.size!=null?kb(node.size):''}</span></div>`;
  const kids=(node.children||[]).map(c=>treeHtml(c,depth+1)).join('');
  const more=node.more?`<div class="tf tmore">… ${node.more} more item${node.more===1?'':'s'}</div>`:'';
  const count=(node.children||[]).length+(node.more||0);
  return `<details class="td"${depth<1?' open':''}>
    <summary><span class="tdn">${escT(node.name)}</span><span class="tfs">${count} item${count===1?'':'s'}</span></summary>
    <div class="tkids">${kids}${more}</div>
  </details>`;
}
