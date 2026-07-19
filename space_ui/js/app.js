/* Entry point. Adding a view = create js/views/<name>.js exporting the view
   contract (see core/registry.js), then import + register it here — no
   bundler, so no file globbing; this import list is the one manual step. */
import {registerView,startRegistry} from './core/registry.js';
import {initServerWidget} from './core/server-widget.js';
import {graphView,timeView,sixView,buildModeToggle,graphMode} from './views/atlas.js';
import overviewView from './views/overview.js';
import sessionsView from './views/sessions.js';
import projectsView from './views/projects.js';
import chatView from './views/chat.js';
import experimentView from './views/experiment.js';

/* app-shell bulkhead: a fatal script error logs instead of white-screening */
addEventListener('error',e=>console.error('Space shell error:',e.error||e.message));
addEventListener('unhandledrejection',e=>console.error('Space unhandled rejection:',e.reason));

/* Dashboard: one tab backed by whichever space the topbar switcher selects —
   the sessions telemetry dashboard or the projects view. Mode switches reload
   the page (see atlas.js), so the composition is static per load. The section
   (#view-dashboard) ships a #sesswrap for the sessions renderer; the projects
   renderer wants a bare section, so the wrap is removed in that mode. */
const dashInner=graphMode()==='sessions'?sessionsView:projectsView;
const dashboardView={
  id:'dashboard',label:'Dashboard',order:4,
  async mount(el,ctx){
    const wrap=el&&el.querySelector('#sesswrap');
    if(dashInner===projectsView&&wrap)wrap.remove();
    return dashInner.mount(el,ctx);
  },
  show(){if(dashInner.show)dashInner.show();},
  hide(){if(dashInner.hide)dashInner.hide();}
};

/* legacy deep links from before Sessions + Projects merged into Dashboard */
if(/^#\/(sessions|projects)$/.test(location.hash))history.replaceState(null,'','#/dashboard');

try{
  registerView(overviewView);   /* order 0 — first tab, before Graph */
  registerView(graphView);
  registerView(timeView);
  registerView(sixView);
  registerView(dashboardView);
  registerView(chatView);
  registerView(experimentView);
  buildModeToggle();  /* topbar Projects/Sessions space switcher, every page */
  startRegistry({defaultView:'graph'});
}catch(err){console.error('Space registry failed to start:',err);}

try{initServerWidget();}catch(err){console.error('Server widget failed to start:',err);}
