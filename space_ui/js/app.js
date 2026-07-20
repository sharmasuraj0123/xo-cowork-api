/* Entry point. Adding a view = create js/views/<name>.js exporting the view
   contract (see core/registry.js), then import + register it here — no
   bundler, so no file globbing; this import list is the one manual step. */
import {registerView,startRegistry,switchTo} from './core/registry.js';
import {initServerWidget} from './core/server-widget.js';
import {graphView,timeView,sixView,buildModeToggle,graphMode} from './views/atlas.js';
import overviewView from './views/overview.js';
import sessionsView from './views/sessions.js';
import projectsView from './views/projects.js';
import environmentsView from './views/environments.js';
import chatView from './views/chat.js';

/* app-shell bulkhead: a fatal script error logs instead of white-screening */
addEventListener('error',e=>console.error('Space shell error:',e.error||e.message));
addEventListener('unhandledrejection',e=>console.error('Space unhandled rejection:',e.reason));

/* Dashboard: one tab backed by whichever space the sidebar selects — the
   sessions telemetry dashboard, the environments board (work items +
   teammates), or the projects view. Space switches reload the page (see
   atlas.js), so the composition is static per load. The section
   (#view-dashboard) ships a #sesswrap for the sessions renderer; the other
   renderers want a bare section, so the wrap is removed in those modes. */
const isBoard=graphMode()==='environments';
const dashInner=graphMode()==='sessions'?sessionsView
  :isBoard?environmentsView:projectsView;
/* The whiteboard can host the other spaces' dashboards as cards. Views never
   import each other (registry contract), so app.js — the composition seam —
   hands them over here. Only the environments board consumes this. */
environmentsView.sections={projects:projectsView,sessions:sessionsView};
/* In the environments space this tab IS the whiteboard — its own id, so the
   URL reads #/whiteboard and the pill says Whiteboard. Other spaces keep
   Dashboard. Composition is static per load (space switches reload). */
const dashboardView={
  id:isBoard?'whiteboard':'dashboard',
  label:isBoard?'Whiteboard':'Dashboard',
  order:1,
  async mount(el,ctx){
    const wrap=el&&el.querySelector('#sesswrap');
    if(dashInner!==sessionsView&&wrap)wrap.remove();
    return dashInner.mount(el,ctx);
  },
  show(){if(dashInner.show)dashInner.show();},
  hide(){if(dashInner.hide)dashInner.hide();}
};
/* Whiteboard mode never uses the static dashboard section, but its bundled
   #sesswrap would shadow the whiteboard's Sessions-card wrap (same id, and
   getElementById takes document order) — drop it up front. */
if(isBoard)document.getElementById('sesswrap')?.remove();

/* Legacy deep links: Sessions + Projects merged into Dashboard, and Experiment
   now lives as the right rail plus retained workbench inside Chat. Run this on
   later hash changes too so an old in-app link cannot leave a stale URL. */
function redirectLegacyRoute(){
  const dashId=isBoard?'whiteboard':'dashboard';
  if(/^#\/(sessions|projects)$/.test(location.hash))history.replaceState(null,'','#/'+dashId);
  /* the tab's id follows the space (Whiteboard vs Dashboard) — a hash from
     the other space's load, or an old bookmark, maps to this space's id */
  else if(location.hash==='#/dashboard'&&isBoard)history.replaceState(null,'','#/whiteboard');
  else if(location.hash==='#/whiteboard'&&!isBoard)history.replaceState(null,'','#/dashboard');
  else if(location.hash==='#/experiment'){
    document.documentElement.dataset.openExperiment='true';
    history.replaceState(null,'','#/chat');
    dispatchEvent(new Event('space:open-experiment'));
  }
}
redirectLegacyRoute();
addEventListener('hashchange',redirectLegacyRoute);

try{
  /* Pill: Dashboard (or Whiteboard) · Files · Timeline. Everything else is
     tabless — Chat has its own topbar button (#tab-chat), Six Degrees opens
     from the Timeline header, Overview stays deep-linkable (its tree
     content now lives in Files → List). */
  registerView(dashboardView);
  registerView(graphView);
  registerView(timeView);
  registerView(sixView);
  registerView({...overviewView,hideTab:true});
  registerView({...chatView,hideTab:true});
  buildModeToggle();  /* topbar space pill: Projects / Sessions / Environments */
  startRegistry({defaultView:dashboardView.id});
  document.getElementById('tab-chat')?.addEventListener('click',()=>switchTo('chat'));
}catch(err){console.error('Space registry failed to start:',err);}

try{initServerWidget();}catch(err){console.error('Server widget failed to start:',err);}
