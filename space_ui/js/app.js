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
const dashInner=graphMode()==='sessions'?sessionsView
  :graphMode()==='environments'?environmentsView:projectsView;
const dashboardView={
  id:'dashboard',label:'Dashboard',order:1,
  async mount(el,ctx){
    const wrap=el&&el.querySelector('#sesswrap');
    if(dashInner!==sessionsView&&wrap)wrap.remove();
    return dashInner.mount(el,ctx);
  },
  show(){if(dashInner.show)dashInner.show();},
  hide(){if(dashInner.hide)dashInner.hide();}
};

/* Legacy deep links: Sessions + Projects merged into Dashboard, and Experiment
   now lives as the right rail plus retained workbench inside Chat. Run this on
   later hash changes too so an old in-app link cannot leave a stale URL. */
function redirectLegacyRoute(){
  if(/^#\/(sessions|projects)$/.test(location.hash))history.replaceState(null,'','#/dashboard');
  else if(location.hash==='#/experiment'){
    document.documentElement.dataset.openExperiment='true';
    history.replaceState(null,'','#/chat');
    dispatchEvent(new Event('space:open-experiment'));
  }
}
redirectLegacyRoute();
addEventListener('hashchange',redirectLegacyRoute);

try{
  /* Pill: Dashboard · Files · Timeline. Everything else is tabless —
     Chat has its own topbar button (#tab-chat), Six Degrees opens from the
     Timeline header, Overview stays deep-linkable (its tree content now
     lives in Files → List). */
  registerView(dashboardView);
  registerView(graphView);
  registerView(timeView);
  registerView(sixView);
  registerView({...overviewView,hideTab:true});
  registerView({...chatView,hideTab:true});
  buildModeToggle();  /* left sidebar: Projects / Sessions / Environments */
  startRegistry({defaultView:'dashboard'});
  document.getElementById('tab-chat')?.addEventListener('click',()=>switchTo('chat'));
}catch(err){console.error('Space registry failed to start:',err);}

try{initServerWidget();}catch(err){console.error('Server widget failed to start:',err);}
