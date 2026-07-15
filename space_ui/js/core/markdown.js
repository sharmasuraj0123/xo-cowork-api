/* Mini-markdown for agent output — escape-first so nothing in the source can
   inject HTML. Supported subset: fenced code blocks, inline code, bold,
   italic, http(s) links, #–###### headings, flat - / 1. lists, paragraphs.
   Deliberately not a full parser; agent replies rarely need more, and every
   feature added here is attack/edge surface. */

const escMd=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function inline(s){
  return s
    .replace(/`([^`\n]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,'$1<i>$2</i>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

export function mdToHtml(src){
  const text=String(src??'');
  /* pull fenced blocks out first so nothing inside them is transformed */
  const fences=[];
  const withSlots=text.replace(/```(\w*)[^\n]*\n([\s\S]*?)```/g,(m,lang,code)=>{
    fences.push('<pre class="md-code"'+(lang?' data-lang="'+escMd(lang)+'"':'')+'><code>'+escMd(code)+'</code></pre>');
    return'\x00F'+(fences.length-1)+'\x00';
  });
  const out=[];
  let list=null; /* 'ul' | 'ol' | null */
  const closeList=()=>{if(list){out.push('</'+list+'>');list=null;}};
  let para=[];
  const flushPara=()=>{
    if(para.length){out.push('<p>'+para.join('<br>')+'</p>');para=[];}
  };
  for(const raw of withSlots.split('\n')){
    const line=raw.trimEnd();
    const fence=/^\x00F(\d+)\x00$/.exec(line.trim());
    if(fence){flushPara();closeList();out.push(fences[+fence[1]]);continue;}
    if(!line.trim()){flushPara();closeList();continue;}
    const h=/^(#{1,6})\s+(.*)$/.exec(line);
    if(h){flushPara();closeList();out.push('<h'+h[1].length+'>'+inline(escMd(h[2]))+'</h'+h[1].length+'>');continue;}
    const ul=/^\s*[-*]\s+(.*)$/.exec(line);
    const ol=/^\s*\d+[.)]\s+(.*)$/.exec(line);
    if(ul||ol){
      flushPara();
      const want=ul?'ul':'ol';
      if(list!==want){closeList();out.push('<'+want+'>');list=want;}
      out.push('<li>'+inline(escMd((ul||ol)[1]))+'</li>');
      continue;
    }
    closeList();
    para.push(inline(escMd(line)));
  }
  flushPara();closeList();
  return out.join('');
}
