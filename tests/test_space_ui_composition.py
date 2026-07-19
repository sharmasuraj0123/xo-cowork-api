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

    def test_chat_keeps_both_center_panes_and_switches_visibility(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")

        self.assertIn("function showChatConversation()", chat)
        self.assertIn("chatMainEl.hidden=false", chat)
        self.assertIn("experimentMainEl.hidden=true", chat)
        self.assertIn("function showExperimentConversation()", chat)
        self.assertIn("chatMainEl.hidden=true", chat)
        self.assertIn("experimentMainEl.hidden=false", chat)

    def test_slow_mount_and_polling_are_guarded_by_current_state(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")
        experiment = self._read("space_ui/js/views/experiment.js")

        self.assertIn("if(chatVisible)experimentPanel.show()", chat)
        self.assertIn("while(refreshRequested)", experiment)
        self.assertIn("requestRevision===stateRevision", experiment)
        self.assertIn("shouldActivate=activationRevision===navigationRevision", experiment)

    def test_compact_experiment_rail_is_an_accessible_drawer(self) -> None:
        chat = self._read("space_ui/js/views/chat.js")
        css = self._read("space_ui/css/chat.css")

        self.assertIn('aria-controls="chat-exp-side"', chat)
        self.assertIn('aria-expanded="false"', chat)
        self.assertIn("experimentSideEl.inert=closing", chat)
        self.assertIn("chatCenterEl.inert=covered", chat)
        self.assertIn("event.key==='Escape'&&drawerOpen", chat)
        self.assertIn("@media (max-width:1180px)", css)

    def test_legacy_route_and_parent_links_land_on_chat(self) -> None:
        app = self._read("space_ui/js/app.js")
        experiment = self._read("space_ui/js/views/experiment.js")
        runtime = self._read("services/cowork_agent/experiments/runtime.py")

        self.assertIn("location.hash==='#/experiment'", app)
        self.assertIn("history.replaceState(null,'','#/chat')", app)
        self.assertIn("dataset.openExperiment='true'", app)
        self.assertIn("activateCurrent()", experiment)
        self.assertIn('/space/#/chat"', runtime)


if __name__ == "__main__":
    unittest.main()
