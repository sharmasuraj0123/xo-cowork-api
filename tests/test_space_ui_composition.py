from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SpaceChatExperimentCompositionTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_experiment_controller_is_mounted_inside_chat(self) -> None:
        app = self._read("space_ui/js/app.js")
        chat = self._read("space_ui/js/views/chat.js")
        experiment = self._read("space_ui/js/views/experiment.js")

        self.assertNotIn("registerView(experimentView)", app)
        self.assertNotIn("import experimentView", app)
        self.assertIn("import experimentPanel from './experiment.js'", chat)
        self.assertIn("experimentPanel.mount", chat)
        self.assertNotIn("await experimentPanel.mount", chat)
        self.assertIn('id="chat-exp-side"', chat)
        self.assertIn('id="chat-experiment-main"', chat)
        self.assertNotIn("id:'experiment'", experiment)

    def test_agent_studio_keeps_both_panes_and_switches_from_one_mode_state(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")

        self.assertIn("function showChatConversation()", chat)
        self.assertIn("function showExperimentConversation()", chat)
        self.assertIn("workspaceMode='chat'", chat)
        self.assertIn("workspaceMode='experiment'", chat)
        self.assertIn("chatMainEl.hidden=!chatMode", chat)
        self.assertIn("experimentMainEl.hidden=chatMode", chat)
        self.assertIn('id="chat-mode-chat"', chat)
        self.assertIn('id="chat-mode-experiment"', chat)
        self.assertIn('role="group" aria-label="Agent Studio workspace"', chat)

    def test_slow_mount_and_polling_are_guarded_by_current_state(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")
        experiment = self._read("space_ui/js/views/experiment.js")

        self.assertIn("if(chatVisible)experimentPanel.show()", chat)
        self.assertIn("while(refreshRequested)", experiment)
        self.assertIn("requestRevision===stateRevision", experiment)
        self.assertIn("shouldActivate=activationRevision===navigationRevision", experiment)
        self.assertIn("pendingExperimentOpen&&workspaceMode==='experiment'", chat)

    def test_compact_experiment_rail_is_an_accessible_drawer(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")
        css = self._read("space_ui/css/chat.css")

        self.assertIn('id="chat-exp-rail-toggle"', chat)
        self.assertIn('aria-controls="chat-exp-side"', chat)
        self.assertIn('aria-expanded="false"', chat)
        self.assertIn("experimentSideEl.inert=!showExperimentSide", chat)
        self.assertIn("chatCenterEl.inert=chatCovered||experimentCovered", chat)
        self.assertIn("event.key==='Escape'&&drawerOpen", chat)
        self.assertIn("@media (max-width:980px)", css)

    def test_agent_studio_uses_one_context_rail_and_a_wide_canvas_per_mode(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")
        experiment = self._read("space_ui/js/views/experiment.js")
        css = self._read("space_ui/css/chat.css")

        self.assertIn('class="agent-studio" data-workspace-mode="chat"', chat)
        self.assertIn('.agent-studio[data-workspace-mode="chat"] .chat', css)
        self.assertIn('.agent-studio[data-workspace-mode="experiment"] .chat', css)
        self.assertNotIn("minmax(0,1fr) clamp(340px", css)
        self.assertIn("chatSideEl.hidden=!showChatSide", chat)
        self.assertIn("experimentSideEl.hidden=!showExperimentSide", chat)
        self.assertIn("onActivate('')", experiment)
        self.assertIn("A clean machine for every idea.", experiment)

    def test_legacy_route_and_parent_links_land_on_chat(self) -> None:
        app = self._read("space_ui/js/app.js")
        experiment = self._read("space_ui/js/views/experiment.js")
        runtime = self._read("services/cowork_agent/experiments/runtime.py")

        self.assertIn("location.hash==='#/experiment'", app)
        self.assertIn("history.replaceState(null,'','#/chat')", app)
        self.assertIn("dataset.openExperiment='true'", app)
        self.assertIn("activateCurrent()", experiment)
        self.assertIn('/space/#/chat"', runtime)

    def test_redesigned_chat_has_accessible_navigation_and_composer(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")

        self.assertIn('aria-label="Conversations"', chat)
        self.assertIn('id="chat-side-toggle"', chat)
        self.assertIn('id="chat-side-close"', chat)
        self.assertIn('role="log" aria-live="polite"', chat)
        self.assertIn('for="chat-input"', chat)
        self.assertIn('data-chat-starter=', chat)
        self.assertIn("if(stream)return;", chat)

    def test_experiment_control_center_has_nine_stage_progress_and_back_path(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")
        experiment = self._read("space_ui/js/views/experiment.js")
        css = self._read("space_ui/css/experiment.css")

        self.assertIn("deactivate:showChatConversation", chat)
        self.assertIn('id="exp-back-chat"', experiment)
        self.assertIn('id="exp-rail-close"', experiment)
        self.assertIn("grid-template-columns:repeat(9", css)
        self.assertNotIn("grid-template-columns:repeat(8", css)

    def test_registry_exposes_current_view_for_contextual_chrome(self) -> None:
        registry = self._read("space_ui/js/core/registry.js")
        css = self._read("space_ui/css/chat.css")

        self.assertIn("document.body.dataset.view=id", registry)
        self.assertIn('body[data-view="chat"]', css)

    def test_chat_inherits_the_existing_space_shell_and_palette(self) -> None:
        index = self._read("space_ui/index.html")
        app = self._read("space_ui/js/app.js")
        css = self._read("space_ui/css/chat.css")

        self.assertIn('id="tab-chat" class="chatbtn"', index)
        self.assertIn("registerView({...chatView,hideTab:true})", app)
        self.assertIn("--surface-canvas:var(--bg)", css)
        self.assertIn("font:500 19px/1.2 var(--serif)", css)
        self.assertNotIn("--bg:#0d100d", css)
        self.assertNotIn('body[data-view="chat"] .topbar', css)
        self.assertNotIn('body[data-view="chat"] :is(.modesw,.rootpick){display:none}', css)

if __name__ == "__main__":
    unittest.main()
