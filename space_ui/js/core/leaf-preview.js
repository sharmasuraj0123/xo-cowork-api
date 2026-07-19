/* Leaf content preview — a fast, editor-like surface that opens beside the
   detail panel when a Projects-space leaf is selected. Uses the existing
   /api/files/* read endpoints; never writes. Sessions-space leaves (no real
   filesystem path) are ignored. */
import {API_BASE,withPageQuery} from './api.js';
import {mdToHtml} from './markdown.js';

export const PREVIEW_W=520;
const MAX_TEXT_BYTES=512*1024;
const IMAGE_TAGS=new Set(['PNG','JPG','JPEG','WEBP','GIF','SVG','ICO','BMP','AVIF']);
const MARKDOWN_TAGS=new Set(['MD','MDX','MARKDOWN','RST']);
const TEXT_TAGS=new Set([
  'TS','TSX','JS','JSX','MJS','CJS','JSON','YAML','YML','TOML','HTML','HTM',
  'CSS','SCSS','LESS','PY','RB','GO','RS','JAVA','KT','SWIFT','C','H','CPP',
  'HPP','CS','PHP','SH','BASH','ZSH','SQL','GRAPHQL','GQL','XML','TXT',
  'LOG','ENV','INI','CFG','CONF','LOCK','VUE','SVELTE','ASTRO','CSV','TSV',
  'DOCKERFILE','MAKEFILE','GITIGNORE','EDITORCONFIG','PRETTIERRC','ESLINTRC',
]);

let root=null,bodyEl=null,titleEl=null,metaEl=null,tagEl=null,modeEl=null;
let gen=0,abort=null,openPath=null,mode='auto';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function ensureDom(){
  if(root)return;
  root=document.getElementById('preview');
  if(!root)return;
  titleEl=document.getElementById('preview-title');
  metaEl=document.getElementById('preview-meta');
  tagEl=document.getElementById('preview-tag');
  modeEl=document.getElementById('preview-mode');
  bodyEl=document.getElementById('preview-body');
  document.getElementById('preview-close')?.addEventListener('click',()=>closePreview());
  modeEl?.addEventListener('click',()=>{
    if(!openPath)return;
    mode=mode==='source'?'render':'source';
    modeEl.textContent=mode==='source'?'Render':'Source';
    /* re-render last payload kept on the element */
    const cached=root._payload;
    if(cached)renderPayload(cached);
  });
}

function kindOf(tag,name){
  const t=String(tag||'').toUpperCase();
  const base=String(name||'').toLowerCase();
  if(IMAGE_TAGS.has(t)||/\.(png|jpe?g|webp|gif|svg|ico|bmp|avif)$/.test(base))return 'image';
  if(MARKDOWN_TAGS.has(t)||/\.(md|mdx|markdown|rst)$/.test(base))return 'markdown';
  if(t==='PDF'||/\.pdf$/.test(base))return 'pdf';
  if(TEXT_TAGS.has(t)||/\.[a-z0-9]{1,8}$/.test(base))return 'text';
  return 'unknown';
}

function resolveAbsPath(node,workspace){
  const rel=String(node?.path||'').trim();
  if(!rel||!workspace||workspace[0]!=='/')return null;
  /* Sessions leaves use a short non-file path (e.g. "home-coder"). */
  if(!rel.includes('/'))return null;
  const joined=workspace.replace(/\/$/,'')+'/'+rel.replace(/^\//,'');
  return joined;
}

async function fetchText(path){
  const url=withPageQuery((API_BASE||'')+'/api/files/content');
  const ctrl=new AbortController();
  abort=ctrl;
  const r=await fetch(url,{
    method:'POST',cache:'no-store',signal:ctrl.signal,
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path}),
  });
  if(!r.ok){
    let message='http '+r.status;
    try{
      const j=await r.json();
      if(typeof j.detail==='string')message=j.detail;
      else if(j.detail?.message)message=j.detail.message;
    }catch(_e){}
    throw new Error(message);
  }
  const data=await r.json();
  const content=String(data.content??'');
  if(content.length>MAX_TEXT_BYTES){
    return{content:content.slice(0,MAX_TEXT_BYTES),truncated:true,bytes:content.length};
  }
  return{content,truncated:false,bytes:content.length};
}

async function fetchImageUrl(path){
  const url=withPageQuery((API_BASE||'')+'/api/files/content-binary');
  const ctrl=new AbortController();
  abort=ctrl;
  const r=await fetch(url,{
    method:'POST',cache:'no-store',signal:ctrl.signal,
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path}),
  });
  if(!r.ok)throw new Error('http '+r.status);
  const blob=await r.blob();
  return URL.createObjectURL(blob);
}

function lineGutter(text){
  const n=Math.max(1,text.split('\n').length);
  let out='';
  for(let i=1;i<=n;i++)out+=i+'\n';
  return out;
}

/* Tiny, dependency-free tint — escape first, park strings/comments in
   placeholders, then keyword-tint the remainder so markup never matches
   words like `class` inside span attributes. */
function tintCode(src,tag){
  const t=String(tag||'').toUpperCase();
  let html=esc(src);
  if(t==='JSON'){
    html=html
      .replace(/(&quot;[^&]*&quot;)(\s*:)/g,'<span class="tok-key">$1</span>$2')
      .replace(/:\s*(&quot;[^&]*&quot;)/g,': <span class="tok-str">$1</span>')
      .replace(/:\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/g,': <span class="tok-num">$1</span>')
      .replace(/\b(true|false|null)\b/g,'<span class="tok-kw">$1</span>');
    return html;
  }
  const slots=[];
  const park=(match,cls)=>{
    const i=slots.length;
    slots.push('<span class="'+cls+'">'+match+'</span>');
    return'\x00'+i+'\x00';
  };
  if(/^(TS|TSX|JS|JSX|MJS|CJS|CSS|SCSS|JAVA|GO|RS|C|CPP|CS|PHP|SWIFT|KT)$/.test(t))
    html=html.replace(/(\/\/[^\n]*)/g,m=>park(m,'tok-com'));
  if(/^(PY|YAML|YML|TOML|SH|BASH|ZSH)$/.test(t))
    html=html.replace(/(#[^\n]*)/g,m=>park(m,'tok-com'));
  html=html.replace(/(&quot;[^&]*&quot;|&#39;[^&#]*&#39;|`[^`]*`)/g,m=>park(m,'tok-str'));
  const kws=
    /^(PY)$/.test(t)?'\\b(def|class|return|import|from|as|if|elif|else|for|while|with|try|except|yield|async|await|True|False|None|lambda|pass|raise|in|not|and|or)\\b':
    /^(TS|TSX|JS|JSX|MJS|CJS)$/.test(t)?'\\b(const|let|var|function|return|import|export|from|as|if|else|for|while|class|extends|new|async|await|typeof|interface|type|enum|default|null|undefined|true|false)\\b':
    /^(CSS|SCSS|LESS)$/.test(t)?'\\b(var|@media|@import|@keyframes|from|to)\\b':
    null;
  if(kws)html=html.replace(new RegExp(kws,'g'),'<span class="tok-kw">$1</span>');
  return html.replace(/\x00(\d+)\x00/g,(_,i)=>slots[+i]||'');
}

function setChrome(node,kind){
  titleEl.textContent=node.label||'Untitled';
  metaEl.textContent=node.path||'';
  tagEl.textContent=node.tag||kind.toUpperCase();
  const showMode=kind==='markdown';
  modeEl.hidden=!showMode;
  if(showMode){
    if(mode!=='source'&&mode!=='render')mode='render';
    modeEl.textContent=mode==='source'?'Render':'Source';
  }
}

function showState(html){
  bodyEl.innerHTML=html;
}

function renderEditor(text,tag,{truncated=false,bytes=0}={}){
  const gutter=lineGutter(text);
  const code=tintCode(text,tag);
  showState(
    (truncated?`<div class="pv-banner">Showing first ${(MAX_TEXT_BYTES/1024)|0} KB of ${(bytes/1024).toFixed(0)} KB</div>`:'')
    +`<div class="pv-editor" role="textbox" aria-readonly="true">`
    +`<pre class="pv-gutter" aria-hidden="true">${gutter}</pre>`
    +`<pre class="pv-code"><code>${code}</code></pre>`
    +`</div>`
  );
}

function renderMarkdown(text,{truncated=false,bytes=0}={}){
  if(mode==='source'){
    renderEditor(text,'MD',{truncated,bytes});
    return;
  }
  showState(
    (truncated?`<div class="pv-banner">Showing first ${(MAX_TEXT_BYTES/1024)|0} KB of ${(bytes/1024).toFixed(0)} KB</div>`:'')
    +`<div class="pv-md md">${mdToHtml(text)}</div>`
  );
}

function renderPayload(payload){
  root._payload=payload;
  const{kind,tag,text,truncated,bytes,imageUrl}=payload;
  if(kind==='image'&&imageUrl){
    showState(`<div class="pv-image"><img src="${esc(imageUrl)}" alt="${esc(titleEl.textContent)}"></div>`);
    return;
  }
  if(kind==='markdown'){
    renderMarkdown(text,{truncated,bytes});
    return;
  }
  if(kind==='text'||kind==='pdf'&&text){
    renderEditor(text,tag,{truncated,bytes});
    return;
  }
  if(kind==='pdf'){
    showState(`<div class="pv-empty"><b>PDF</b><span>Binary preview stays in the detail panel path — open the file on disk to read pages.</span></div>`);
    return;
  }
  showState(`<div class="pv-empty"><b>No preview</b><span>This file type isn’t shown as text or an image.</span></div>`);
}

export function previewWidth(){
  return root?.classList.contains('is-open')?PREVIEW_W:0;
}

export function isPreviewOpen(){
  return!!root?.classList.contains('is-open');
}

export function closePreview(){
  ensureDom();
  if(!root)return;
  gen++;
  if(abort){try{abort.abort();}catch(_e){}abort=null;}
  const prev=root._payload?.imageUrl;
  if(prev)URL.revokeObjectURL(prev);
  root._payload=null;
  openPath=null;
  mode='auto';
  root.classList.remove('is-open');
  bodyEl&&(bodyEl.innerHTML='');
}

export async function openLeafPreview(node,{workspace}={}){
  ensureDom();
  if(!root||!node||node.type!=='leaf'){closePreview();return false;}
  const abs=resolveAbsPath(node,workspace);
  if(!abs){closePreview();return false;}

  const kind=kindOf(node.tag,node.label);
  const my=++gen;
  if(abort){try{abort.abort();}catch(_e){}}
  /* revoke previous blob if path changes */
  if(root._payload?.imageUrl&&openPath!==abs){
    URL.revokeObjectURL(root._payload.imageUrl);
  }
  openPath=abs;
  setChrome(node,kind);
  if(kind==='markdown'&&mode==='auto')mode='render';
  root.classList.add('is-open');
  showState(`<div class="pv-empty pv-loading"><span class="pv-pulse"></span><span>Loading preview…</span></div>`);

  try{
    if(kind==='image'){
      const imageUrl=await fetchImageUrl(abs);
      if(my!==gen){URL.revokeObjectURL(imageUrl);return true;}
      renderPayload({kind,tag:node.tag,imageUrl});
      return true;
    }
    if(kind==='unknown'){
      if(my!==gen)return true;
      renderPayload({kind,tag:node.tag});
      return true;
    }
    if(kind==='pdf'){
      if(my!==gen)return true;
      renderPayload({kind,tag:node.tag});
      return true;
    }
    const{content,truncated,bytes}=await fetchText(abs);
    if(my!==gen)return true;
    renderPayload({kind,tag:node.tag,text:content,truncated,bytes});
    return true;
  }catch(err){
    if(my!==gen||err?.name==='AbortError')return false;
    showState(`<div class="pv-empty"><b>Couldn’t open</b><span>${esc(err.message||'Unknown error')}</span></div>`);
    return false;
  }
}
