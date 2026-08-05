/* Entry point. Adding a view = create js/views/<name>.js exporting the view
   contract (see core/registry.js), then import + register it here — no
   bundler, so no file globbing; this import list is the one manual step. */
import {registerView,startRegistry} from './core/registry.js';
import {initServerWidget} from './core/server-widget.js';
import {graphView,timeView,sixView} from './views/atlas.js';
import sessionsView from './views/sessions.js';
import projectsView from './views/projects.js';
import chatView from './views/chat.js';

/* app-shell bulkhead: a fatal script error logs instead of white-screening */
addEventListener('error',e=>console.error('Space shell error:',e.error||e.message));
addEventListener('unhandledrejection',e=>console.error('Space unhandled rejection:',e.reason));

try{
  registerView(graphView);
  registerView(timeView);
  registerView(sixView);
  registerView(sessionsView);
  registerView(projectsView);
  registerView(chatView);
  startRegistry({defaultView:'graph'});
}catch(err){console.error('Space registry failed to start:',err);}

try{initServerWidget();}catch(err){console.error('Server widget failed to start:',err);}
