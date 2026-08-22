/* Workspace-wide rollups, in one request.

   The Files List needs a file and folder count per project. Asking each
   project for its tree would be one request per row; `.xo/space.json` — the
   same bounded, server-cached payload the Graph and Tree lenses read — already
   carries every mapped file as "<project>/<relative path>", so the whole
   column costs one fetch no matter how many projects there are.

   Lives in core/ because two views need it and views never import each other.
   Counts are of MAPPED files: space_index caps its scan at 400 leaves per
   project and 1500 workspace-wide, so a big project reports its capped
   number. The cap is reported PER PROJECT (`p.capped`) and separately for
   the workspace total (`totalsCapped`) — a single workspace-wide flag would
   put a "+" on an 18-file project the moment some other project filled the
   graph, which is worse than showing no number at all. */
import {API_BASE,apiFetch} from './api.js';

export async function workspaceCounts(){
  const res=await apiFetch(API_BASE+'/xo/space.json');
  if(!res.ok)return{ok:false,error:res.error,offline:res.offline,byProject:new Map(),
    totals:{projects:0,files:0,folders:0}};

  const byProject=new Map();
  const ensure=id=>{
    if(!byProject.has(id))byProject.set(id,{files:0,folders:0,dirs:new Set()});
    return byProject.get(id);
  };
  for(const hub of res.data.hubs||[])ensure(hub.id.replace(/^p_/,''));
  for(const leaf of res.data.leaves||[]){
    const parts=String(leaf.path||'').split('/').filter(Boolean);
    if(parts.length<2)continue;
    const p=ensure(parts[0]);
    p.files++;
    /* every ancestor directory of this file, deduped */
    for(let i=1;i<parts.length-1;i++)p.dirs.add(parts.slice(1,i+1).join('/'));
  }
  let files=0,folders=0;
  for(const p of byProject.values()){
    p.folders=p.dirs.size;
    delete p.dirs;
    /* MAX_LEAVES_PER_PROJECT in space_index.py */
    p.capped=p.files>=400;
    files+=p.files;folders+=p.folders;
  }
  return{ok:true,byProject,
    totals:{projects:byProject.size,files,folders},
    /* MAX_TOTAL_LEAVES in space_index.py */
    totalsCapped:(res.data.leaves||[]).length>=1500};
}
