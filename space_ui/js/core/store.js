/* Shared idempotency helpers. Not a data model — just the guards that make
   repeated operations converge instead of accumulate. */

/* Single-flight: concurrent callers for the same key share one in-flight
   promise (one network round-trip, one resolution order — no lost-update
   races between duplicate requests). The entry is deleted on settle, so the
   NEXT call is a genuinely fresh request — live data stays live. */
const inflight=new Map();
export function singleFlight(key,fetcher){
  if(inflight.has(key))return inflight.get(key);
  const p=Promise.resolve().then(fetcher).finally(()=>inflight.delete(key));
  inflight.set(key,p);
  return p;
}

/* Slotted timers: starting a named interval replaces the previous one, so a
   poll can never stack into two (or ten) concurrent intervals. */
const slots=new Map();
export function setSlottedInterval(name,fn,ms){
  clearSlottedInterval(name);
  slots.set(name,setInterval(fn,ms));
}
export function clearSlottedInterval(name){
  if(slots.has(name)){clearInterval(slots.get(name));slots.delete(name);}
}
