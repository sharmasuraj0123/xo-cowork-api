/* One fetch layer for the whole UI.
   - API_BASE: served under /space/ means THIS origin is the API — true on
     localhost and equally true behind a remote proxy (e.g. a Coder workspace
     URL), so talk same-origin and inherit the page's routing + auth. The
     127.0.0.1:5002 fallback exists only for standalone UI dev, where the
     page is opened from some other static server.
   - Every request forwards the page's query string (e.g. Coder's
     ?coder_session_token=…) so each fetch authenticates on its own.
   - Never throws. Returns {ok, status, data, error, offline, notImplemented}:
       offline        — the request never reached the server (fetch TypeError:
                        down/restarting/proxy). Must not be blamed on the API.
       notImplemented — HTTP 501: the active agent lacks the capability. A
                        normal state for callers to render, not an error.
       error          — the API's own explanation when it sent one (a JSON
                        body with detail.message / detail), else "http NNN".
   - Concurrent GETs for the same path share one in-flight request
     (single-flight); sequential calls always hit the network fresh. */
import {singleFlight} from './store.js';

export const API_BASE=location.pathname.startsWith('/space')?'':'http://127.0.0.1:5002';

async function doFetch(path,method){
  try{
    const r=await fetch(path+location.search,{method,cache:'no-store'});
    if(!r.ok){
      let message='http '+r.status;
      try{
        const j=await r.json();
        if(j.detail&&j.detail.message)message=j.detail.message;
        else if(typeof j.detail==='string')message=j.detail;
      }catch(e){}
      return{ok:false,status:r.status,data:null,offline:false,notImplemented:r.status===501,error:message};
    }
    let data=null;
    try{data=await r.json();}
    catch(err){return{ok:false,status:r.status,data:null,offline:false,notImplemented:false,error:err.message};}
    return{ok:true,status:r.status,data,offline:false,notImplemented:false,error:null};
  }catch(err){
    return{ok:false,status:0,data:null,offline:err instanceof TypeError,notImplemented:false,error:err.message};
  }
}

export function apiFetch(path,{method='GET'}={}){
  if(method==='GET')return singleFlight('GET '+path,()=>doFetch(path,'GET'));
  return doFetch(path,method); /* writes are never deduped — disable the button instead */
}
