/* Shared Files-List renderer: the collapsible contents tree used by the
   Files tab's List mode and hosted as a card on the whiteboard. Lives in
   core because views must not import each other (registry contract).

   Fetches the overview payload (workspace tree, or per-runtime session
   stores when {sessions:true}) and renders .ovcard/.ovtree markup — tree
   glyph styles come from overview.css, which is loaded globally.
   Returns true when content rendered, false on failure (callers may retry). */
import {apiFetch} from './api.js';
import {treeHtml} from './ui.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

export async function renderFilesList(el,{sessions=false}={}){
  if(!el)return false;
  el.innerHTML='<div class="ovload">reading contents…</div>';
  const res=await apiFetch(sessions?'data/overview_sessions.json':'data/overview.json');
  if(!res.ok){
    el.innerHTML='<div class="ovload">Contents unavailable · '+esc(res.error||'')+'</div>';
    return false;
  }
  if(sessions){
    const sources=res.data.sources||[];
    el.innerHTML=sources.map(s=>(s.roots||[]).map(r=>`
      <div class="ovcard wide treecard"><h4>${esc(s.label)} · ${esc(r.label)}</h4>
        <div class="ovsub mono">${esc(r.path)}</div>
        <div class="ovtree">${(r.tree.children||[]).map(c=>treeHtml(c,0)).join('')||'<div class="xempty">Empty.</div>'}</div>
      </div>`).join('')).join('')||'<div class="ovload">No session data stores found.</div>';
  }else{
    const t=res.data.tree;
    el.innerHTML=`<div class="ovcard wide treecard"><h4>${esc((res.data.root||'').split('/').pop()||'workspace')} — contents</h4>
      <div class="ovtree">${t?((t.children||[]).map(c=>treeHtml(c,0)).join('')||'<div class="xempty">Empty.</div>'):'<div class="xempty">No tree.</div>'}</div></div>`;
  }
  return true;
}
