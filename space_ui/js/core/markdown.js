/* Mini-markdown for agent output — escape-first so nothing in the source can
   inject HTML. Supported subset: fenced code blocks, inline code, bold,
   italic, strikethrough, http(s) links, #–###### headings, flat - / 1. lists,
   task lists, blockquotes, horizontal rules, GFM tables, paragraphs.
   Deliberately not a full parser; agent replies rarely need more, and every
   feature added here is attack/edge surface — all text is escaped before any
   transform, and only fixed, attribute-free tags are emitted.
   Known limitation: lists are flat (nested/indented items render at one level). */

const escMd=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function inline(s){
  return s
    .replace(/`([^`\n]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
    .replace(/~~([^~\n]+)~~/g,'<s>$1</s>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,'$1<i>$2</i>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

/* A |---|:-:|---| row: all cells are dashes with optional : alignment marks,
   and there are at least two columns (one pipe separating them). */
const isTableSep=s=>/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(s);

/* Split a table row on unescaped pipes; a literal cell pipe is written `\|`. */
function tableRow(s){
  const t=s.trim().replace(/^\|/,'').replace(/\|$/,'');
  return t.split(/(?<!\\)\|/).map(c=>c.replace(/\\\|/g,'|').trim());
}

const cell=(tag,text)=>'<'+tag+'>'+inline(escMd(text))+'</'+tag+'>';

export function mdToHtml(src){
  const text=String(src??'');
  /* pull fenced blocks out first so nothing inside them is transformed */
  const fences=[];
  const withSlots=text.replace(/```(\w*)[^\n]*\n([\s\S]*?)```/g,(m,lang,code)=>{
    fences.push('<pre class="md-code"'+(lang?' data-lang="'+escMd(lang)+'"':'')+'><code>'+escMd(code)+'</code></pre>');
    return'\x00F'+(fences.length-1)+'\x00';
  });
  const lines=withSlots.split('\n');
  const isSlot=s=>/^\x00F\d+\x00$/.test(String(s).trim());
  const out=[];
  let list=null; /* 'ul' | 'ol' | null */
  const closeList=()=>{if(list){out.push('</'+list+'>');list=null;}};
  let para=[];
  const flushPara=()=>{if(para.length){out.push('<p>'+para.join('<br>')+'</p>');para=[];}};

  for(let i=0;i<lines.length;i++){
    const line=lines[i].trimEnd();
    const t=line.trim();

    const fence=/^\x00F(\d+)\x00$/.exec(t);
    if(fence){flushPara();closeList();out.push(fences[+fence[1]]);continue;}

    if(!t){flushPara();closeList();continue;}

    /* GFM table: a pipe row immediately followed by a |---|---| separator */
    if(t.includes('|')&&i+1<lines.length&&isTableSep(lines[i+1])){
      flushPara();closeList();
      const head=tableRow(t);
      i++; /* consume the separator line */
      const body=[];
      while(i+1<lines.length){
        const nxt=lines[i+1];
        if(!nxt.trim()||!nxt.includes('|')||isSlot(nxt))break;
        body.push(tableRow(nxt));i++;
      }
      let h='<table class="md-table"><thead><tr>'+head.map(c=>cell('th',c)).join('')+'</tr></thead>';
      if(body.length)
        h+='<tbody>'+body.map(r=>'<tr>'+head.map((_,x)=>cell('td',r[x]??'')).join('')+'</tr>').join('')+'</tbody>';
      out.push(h+'</table>');
      continue;
    }

    /* horizontal rule: ---, ***, ___ (3+, optionally spaced) */
    if(/^([-*_])(\s*\1){2,}$/.test(t)){flushPara();closeList();out.push('<hr>');continue;}

    /* heading */
    const h=/^(#{1,6})\s+(.*)$/.exec(line);
    if(h){flushPara();closeList();out.push('<h'+h[1].length+'>'+inline(escMd(h[2]))+'</h'+h[1].length+'>');continue;}

    /* blockquote: merge consecutive > lines into one quote */
    if(/^\s*>\s?/.test(line)){
      flushPara();closeList();
      const q=[];let j=i;
      while(j<lines.length&&/^\s*>\s?/.test(lines[j])){
        q.push(inline(escMd(lines[j].replace(/^\s*>\s?/,''))));j++;
      }
      i=j-1;
      out.push('<blockquote>'+q.join('<br>')+'</blockquote>');
      continue;
    }

    /* task-list item (checked before plain list so [-] doesn't win) */
    const task=/^\s*[-*]\s+\[([ xX])\]\s+(.*)$/.exec(line);
    if(task){
      flushPara();
      if(list!=='ul'){closeList();out.push('<ul>');list='ul';}
      const on=task[1].toLowerCase()==='x';
      out.push('<li class="md-task"><input type="checkbox" disabled'+(on?' checked':'')+'> '+inline(escMd(task[2]))+'</li>');
      continue;
    }

    /* flat unordered / ordered list */
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
