from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.cowork_agent.adapters.codex import session_telemetry as codex_stats
from services.cowork_agent.adapters.cursor import session_telemetry as cursor_stats
from services.cowork_agent.adapters import loader as capability_loader
from services.cowork_agent.adapters.loader import list_capability_providers
from services.cowork_agent.registry.adapter_registry import list_adapters
from services.cowork_agent.visualizer.argus_index import build_argus_stats
from services.cowork_agent.visualizer import session_telemetry as combined_stats


def _token_event(timestamp: str, input_tokens: int, cached: int, output: int) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": 1,
                    "total_tokens": input_tokens + output,
                }
            },
        },
    }


class CodexSessionTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        codex_stats._ROLLOUT_CACHE.clear()

    def _write_rollout(self, path: Path, events: list[dict], partial: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(event) + "\n" for event in events)
        if partial:
            text += '{"timestamp":"2026-07-19T02:00:00Z","type":"event_msg"'
        path.write_text(text, encoding="utf-8")

    def _create_state(self, root: Path, rows: list[tuple], edges: list[tuple]) -> None:
        connection = sqlite3.connect(root / "state_7.sqlite")
        connection.executescript(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                cwd text not null,
                tokens_used integer not null,
                cli_version text not null,
                model text,
                title text,
                first_user_message text,
                preview text
            );
            create table thread_spawn_edges (
                parent_thread_id text not null,
                child_thread_id text not null,
                status text not null
            );
            create table _sqlx_migrations (
                version integer primary key,
                success boolean not null
            );
            insert into _sqlx_migrations values (7, 1);
            """
        )
        connection.executemany(
            "insert into threads values (?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        connection.executemany(
            "insert into thread_spawn_edges values (?,?,?)", edges
        )
        connection.commit()
        connection.close()

    def test_collects_cumulative_tokens_tools_and_subagents_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root_rollout = root / "sessions/2026/07/18/root.jsonl"
            child_rollout = root / "sessions/2026/07/19/child.jsonl"
            root_events = [
                {
                    "timestamp": "2026-07-18T23:00:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-test", "turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-07-18T23:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-07-18T23:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": None},
                },
                _token_event("2026-07-18T23:00:03Z", 100, 40, 10),
                _token_event("2026-07-18T23:00:04Z", 100, 40, 10),
                {
                    "timestamp": "2026-07-18T23:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call", "call_id": "call-1",
                        "name": "exec", "arguments": "DO_NOT_LEAK_ARGUMENTS",
                    },
                },
                {
                    "timestamp": "2026-07-18T23:00:06Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "exec_command_end", "call_id": "call-1",
                        "exit_code": 1, "output": "DO_NOT_LEAK_OUTPUT",
                    },
                },
                {
                    "timestamp": "2026-07-18T23:00:07Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call", "call_id": "call-2",
                        "namespace": "mcp__node_repl", "name": "js",
                        "arguments": "DO_NOT_LEAK_NAMESPACED_ARGUMENTS",
                    },
                },
                {
                    "timestamp": "2026-07-18T23:00:08Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "mcp_tool_call_end", "call_id": "call-2",
                        "invocation": {"server": "node_repl", "tool": "js"},
                        "result": {"Ok": "DO_NOT_LEAK_NAMESPACED_OUTPUT"},
                    },
                },
                _token_event("2026-07-19T00:00:01Z", 150, 60, 15),
                {
                    "timestamp": "2026-07-19T00:00:02Z",
                    "type": "response_item",
                    "payload": {"type": "message", "text": "DO_NOT_LEAK_PROMPT"},
                },
            ]
            child_events = [
                {
                    "timestamp": "2026-07-19T00:10:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-test", "turn_id": "turn-c"},
                },
                {
                    "timestamp": "2026-07-19T00:10:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-c"},
                },
                _token_event("2026-07-19T00:10:02Z", 50, 20, 5),
            ]
            self._write_rollout(root_rollout, root_events, partial=True)
            self._write_rollout(child_rollout, child_events)
            self._create_state(
                root,
                [
                    ("root", str(root_rollout), 1000, 2000, "/work/project", 165,
                     "1.2.3", "gpt-test", "SECRET TITLE", "SECRET USER", "SECRET PREVIEW"),
                    ("child", str(child_rollout), 1100, 1900, "/work/project", 55,
                     "1.2.3", "gpt-test", "CHILD TITLE", "CHILD USER", "CHILD PREVIEW"),
                ],
                [("root", "child", "open")],
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                data = codex_stats.collect_session_telemetry()

            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 220)
            self.assertEqual(sum(row["tokens"] for row in data["daily_models"]), 220)
            self.assertEqual(sum(row["tokens"] for row in data["daily_sessions"]), 220)
            self.assertFalse(data["daily_models"][0]["cost_known"])

            session = data["sessions"][0]
            self.assertEqual(session["key"], "codex:root")
            self.assertEqual(session["fresh"], 120)
            self.assertEqual(session["cache_read"], 80)
            self.assertEqual(session["output"], 20)
            self.assertEqual(session["tokens"], 220)
            self.assertEqual(session["own_tokens"], 165)
            self.assertEqual(session["total_tokens"], 220)
            self.assertEqual(session["unclassified"], 0)
            self.assertTrue(session["breakdown_known"])
            self.assertEqual(session["turns"], 1)
            self.assertFalse(session["cost_known"])
            self.assertEqual(session["subagents"][0]["id"], "child")
            self.assertEqual(session["subagents"][0]["tokens"], 55)
            self.assertEqual(session["tools"], [
                {"name": "exec", "calls": 1, "errors": 1},
                {"name": "mcp__node_repl__js", "calls": 1, "errors": 0},
            ])

            serialized = json.dumps(data)
            for secret in (
                "DO_NOT_LEAK_ARGUMENTS", "DO_NOT_LEAK_OUTPUT", "DO_NOT_LEAK_PROMPT",
                "DO_NOT_LEAK_NAMESPACED_ARGUMENTS", "DO_NOT_LEAK_NAMESPACED_OUTPUT",
                "SECRET TITLE", "SECRET USER", "SECRET PREVIEW",
            ):
                self.assertNotIn(secret, serialized)

    def test_invalid_rollout_falls_back_to_state_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.jsonl"
            self._write_rollout(outside, [_token_event("2026-07-19T00:00:00Z", 999, 0, 0)])
            self._create_state(
                root,
                [("orphan", str(outside), 1000, 2000, "/work/fallback", 20,
                  "1.0", "gpt-fallback", "title", "message", "preview")],
                [],
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                data = codex_stats.collect_session_telemetry()
            self.assertEqual(data["source"]["invalid_rollouts"], 1)
            self.assertEqual(data["totals"]["tokens"], 20)
            session = data["sessions"][0]
            self.assertEqual(session["tokens"], 20)
            self.assertEqual(session["fresh"], 0)
            self.assertEqual(session["unclassified"], 20)
            self.assertFalse(session["breakdown_known"])

    def test_reconciles_valid_rollout_without_usage_to_state_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "sessions/2026/07/19/active.jsonl"
            self._write_rollout(
                rollout,
                [{
                    "timestamp": "2026-07-19T00:00:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-active", "turn_id": "turn-1"},
                }],
                partial=True,
            )
            self._create_state(
                root,
                [("active", str(rollout), 1000, 2000, "/work/active", 25,
                  "1.0", "gpt-active", "title", "message", "preview")],
                [],
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                data = codex_stats.collect_session_telemetry()

            session = data["sessions"][0]
            self.assertEqual(data["totals"]["tokens"], 25)
            self.assertEqual(sum(row["tokens"] for row in data["daily_models"]), 25)
            self.assertEqual(session["tokens"], 25)
            self.assertEqual(session["unclassified"], 25)
            self.assertFalse(session["breakdown_known"])

    def test_uses_newer_parsed_total_when_rollout_is_ahead_of_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "sessions/2026/07/19/ahead.jsonl"
            self._write_rollout(
                rollout, [_token_event("2026-07-19T00:00:00Z", 17, 5, 3)]
            )
            self._create_state(
                root,
                [("ahead", str(rollout), 1000, 2000, "/work/ahead", 10,
                  "1.0", "gpt-ahead", "title", "message", "preview")],
                [],
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                data = codex_stats.collect_session_telemetry()

            session = data["sessions"][0]
            self.assertEqual(data["totals"]["tokens"], 20)
            self.assertEqual(sum(row["tokens"] for row in data["daily_models"]), 20)
            self.assertEqual(session["tokens"], 20)
            self.assertEqual(session["fresh"], 12)
            self.assertEqual(session["cache_read"], 5)
            self.assertEqual(session["output"], 3)
            self.assertEqual(session["unclassified"], 0)
            self.assertTrue(session["breakdown_known"])

    def test_keeps_zero_token_parent_when_child_has_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_rollout = root / "sessions/2026/07/19/parent.jsonl"
            child_rollout = root / "sessions/2026/07/19/child.jsonl"
            self._write_rollout(parent_rollout, [])
            self._write_rollout(
                child_rollout,
                [_token_event("2026-07-19T00:10:00Z", 10, 0, 0)],
            )
            self._create_state(
                root,
                [
                    ("parent", str(parent_rollout), 1000, 2000, "/work/tree", 0,
                     "1.0", "gpt-test", "title", "message", "preview"),
                    ("child", str(child_rollout), 1100, 1900, "/work/tree", 10,
                     "1.0", "gpt-test", "title", "message", "preview"),
                ],
                [("parent", "child", "closed")],
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                data = codex_stats.collect_session_telemetry()

            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["sessions"][0]["id"], "parent")
            self.assertEqual(data["sessions"][0]["subagents"][0]["id"], "child")
            self.assertEqual(data["sessions"][0]["own_tokens"], 0)
            self.assertEqual(data["sessions"][0]["total_tokens"], 10)
            self.assertEqual(data["totals"]["tokens"], 10)

    def test_recovers_recent_rollout_ahead_of_zero_state_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "sessions/2026/07/19/just-started.jsonl"
            self._write_rollout(
                rollout, [_token_event("2026-07-19T00:00:00Z", 10, 0, 2)]
            )
            self._create_state(
                root,
                [("just-started", str(rollout), 1000, 2000, "/work/new", 0,
                  "1.0", "gpt-new", "title", "message", "preview")],
                [],
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                data = codex_stats.collect_session_telemetry()

            self.assertEqual(data["totals"]["sessions"], 1)
            self.assertEqual(data["totals"]["tokens"], 12)
            self.assertEqual(data["sessions"][0]["tokens"], 12)
            self.assertEqual(data["source"]["zero_token_roots_recovered"], 1)

    def test_uses_newest_schema_compatible_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "sessions/2026/07/19/session.jsonl"
            self._write_rollout(
                rollout, [_token_event("2026-07-19T00:00:00Z", 10, 0, 0)]
            )
            self._create_state(
                root,
                [("session", str(rollout), 1000, 2000, "/work/good", 10,
                  "1.0", "gpt-test", "title", "message", "preview")],
                [],
            )
            incompatible = sqlite3.connect(root / "state_99.sqlite")
            incompatible.execute("create table migration_in_progress (id integer)")
            incompatible.commit()
            incompatible.close()

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                data = codex_stats.collect_session_telemetry()
            self.assertTrue(data["meta"]["db_path"].endswith("state_7.sqlite"))


class ArgusSessionTelemetryTests(unittest.TestCase):
    def test_preserves_agent_dimension_in_every_rollup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "argus.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                create table app_meta (key text primary key, value text);
                insert into app_meta values ('schema_version', '6');
                create table sessions (
                    id text primary key, agent text not null, project_path text,
                    started_at text, ended_at text, duration_sec integer,
                    total_fresh_input_tokens integer,
                    total_output_tokens integer,
                    total_cache_read_tokens integer,
                    total_cache_write_tokens integer,
                    total_cost_usd real, primary_model text, turn_count integer,
                    agent_version text, pricing_table_version text
                );
                create table turns (
                    id text primary key, session_id text, timestamp text, model text,
                    fresh_input_tokens integer, output_tokens integer,
                    cache_read_tokens integer, cache_write_tokens integer,
                    cost_usd real
                );
                create table tool_calls (
                    id text primary key, session_id text, timestamp text,
                    tool_name text, is_error integer
                );
                """
            )
            connection.execute(
                "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("parent", "claude_code", "/work/alpha", "2026-07-18T00:00:00Z",
                 "2026-07-18T00:10:00Z", 600, 10, 5, 20, 0, 0.5,
                 "claude-test", 1, "2.0", "pricing-v1"),
            )
            connection.execute(
                "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("foreign", "codex", "/work/foreign", "2026-07-18T01:00:00Z",
                 "2026-07-18T01:10:00Z", 600, 100, 50, 200, 0, 0.0,
                 "gpt-test", 1, "1.0", None),
            )
            connection.execute(
                "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("parent/agent-child", "claude_code", "/work/alpha",
                 "2026-07-18T00:02:00Z", "2026-07-18T00:03:00Z", 60,
                 5, 0, 0, 0, 0.05, "claude-test", 1, "2.0", "pricing-v1"),
            )
            connection.execute(
                "insert into turns values (?,?,?,?,?,?,?,?,?)",
                ("turn", "parent", "2026-07-18T00:01:00Z", "claude-test",
                 10, 0, 20, 0, 0.45),
            )
            connection.execute(
                "insert into turns values (?,?,?,?,?,?,?,?,?)",
                ("child-turn", "parent/agent-child", "2026-07-18T00:02:30Z",
                 "claude-test", 5, 0, 0, 0, 0.05),
            )
            connection.execute(
                "insert into tool_calls values (?,?,?,?,?)",
                ("tool", "parent", "2026-07-18T00:02:00Z", "Read", 0),
            )
            connection.execute(
                "insert into turns values (?,?,?,?,?,?,?,?,?)",
                ("foreign-turn", "foreign", "2026-07-18T01:01:00Z", "gpt-test",
                 100, 50, 200, 0, 0.0),
            )
            connection.commit()
            connection.close()

            data = build_argus_stats(path)
            by_id = {row["id"]: row for row in data["sessions"]}
            self.assertEqual(by_id["parent"]["agent"], "claude_code")
            self.assertEqual(by_id["parent"]["key"], "claude_code:parent")
            self.assertEqual(by_id["parent"]["tokens"], 35)
            self.assertEqual(by_id["parent"]["total_tokens"], 35)
            self.assertEqual(by_id["parent"]["own_tokens"], 30)
            self.assertEqual(by_id["parent"]["subagents"][0]["tokens"], 5)
            self.assertEqual(
                {row["agent"] for row in data["daily_models"]},
                {"claude_code", "codex"},
            )
            self.assertIn(
                "claude_code:parent",
                {row["session_key"] for row in data["daily_sessions"]},
            )
            self.assertEqual(data["daily_tools"][0]["agent"], "claude_code")
            self.assertEqual(
                data["totals"]["sessions_by_agent"],
                {"claude_code": 1, "codex": 1},
            )
            self.assertEqual(data["project_keys"], ["/work/alpha", "/work/foreign"])

            filtered = build_argus_stats(path, agent="claude_code")
            self.assertEqual([row["id"] for row in filtered["sessions"]], ["parent"])
            self.assertEqual(
                filtered["totals"]["sessions_by_agent"], {"claude_code": 1}
            )
            self.assertEqual(filtered["project_keys"], ["/work/alpha"])

            with mock.patch(
                "services.cowork_agent.visualizer.argus_index.MAX_SESSIONS", 1
            ):
                capped = build_argus_stats(path)
            self.assertEqual(len(capped["sessions"]), 1)
            self.assertEqual(
                capped["project_keys"], ["/work/alpha", "/work/foreign"]
            )


class CombinedSessionTelemetryTests(unittest.TestCase):
    def _contribution(self) -> dict:
        return {
            "source": {"id": "good", "label": "Good", "cost_status": "estimated"},
            "meta_priority": 1,
            "meta": {"db_path": "/tmp/good.db", "pricing_version": "v1"},
            "totals": {"sessions": 1, "tokens": 10, "cost_usd": 1.5},
            "project_keys": ["/work/a"],
            "sessions": [{"id": "same", "key": "good:same", "agent": "good", "started_at": "2026-01-01T00:00:00Z"}],
            "daily_models": [], "daily_sessions": [], "daily_tools": [],
        }

    def test_partial_provider_failure_still_returns_data_and_status(self) -> None:
        good = SimpleNamespace(
            SOURCE_ID="good", SOURCE_LABEL="Good", COST_STATUS="estimated",
            collect_session_telemetry=self._contribution,
        )
        bad = SimpleNamespace(
            SOURCE_ID="bad", SOURCE_LABEL="Bad", COST_STATUS="unavailable",
            collect_session_telemetry=mock.Mock(side_effect=RuntimeError("not ready")),
        )

        def load(_capability: str, *, agent: str):
            return {"good": good, "bad": bad}[agent]

        with mock.patch.object(combined_stats, "list_capability_providers", return_value=["bad", "good"]), mock.patch.object(combined_stats, "try_load_capability", side_effect=load):
            data = combined_stats.build_session_telemetry()

        self.assertEqual(data["totals"]["sessions"], 1)
        self.assertEqual(data["sessions"][0]["key"], "good:same")
        status = {row["id"]: row["status"] for row in data["meta"]["sources"]}
        self.assertEqual(status, {"bad": "unavailable", "good": "available"})

    def test_all_provider_failures_raise(self) -> None:
        bad = SimpleNamespace(
            SOURCE_ID="bad", SOURCE_LABEL="Bad", COST_STATUS="unknown",
            collect_session_telemetry=mock.Mock(side_effect=RuntimeError("broken")),
        )
        with mock.patch.object(combined_stats, "list_capability_providers", return_value=["bad"]), mock.patch.object(combined_stats, "try_load_capability", return_value=bad):
            with self.assertRaises(combined_stats.SessionTelemetryUnavailable):
                combined_stats.build_session_telemetry()

    def test_malformed_provider_is_isolated_from_healthy_source(self) -> None:
        good = SimpleNamespace(
            SOURCE_ID="good", SOURCE_LABEL="Good", COST_STATUS="estimated",
            collect_session_telemetry=self._contribution,
        )
        malformed = SimpleNamespace(
            SOURCE_ID="malformed", SOURCE_LABEL="Malformed",
            COST_STATUS="unavailable",
            collect_session_telemetry=lambda: {
                "source": {"id": "malformed", "label": "Malformed"},
                "totals": {"sessions": 1, "tokens": 10},
                "sessions": [None],
                "daily_models": [], "daily_sessions": [], "daily_tools": [],
            },
        )

        def load(_capability: str, *, agent: str):
            return {"good": good, "malformed": malformed}[agent]

        with mock.patch.object(
            combined_stats, "list_capability_providers",
            return_value=["malformed", "good"],
        ), mock.patch.object(combined_stats, "try_load_capability", side_effect=load):
            data = combined_stats.build_session_telemetry()

        self.assertEqual(data["totals"]["sessions"], 1)
        status = {row["id"]: row for row in data["meta"]["sources"]}
        self.assertFalse(status["malformed"]["available"])
        self.assertNotIn("NoneType", status["malformed"].get("message", ""))

    def test_nonnumeric_provider_rows_are_isolated(self) -> None:
        good = SimpleNamespace(
            SOURCE_ID="good", SOURCE_LABEL="Good", COST_STATUS="estimated",
            collect_session_telemetry=self._contribution,
        )
        malformed = SimpleNamespace(
            SOURCE_ID="numeric_bad", SOURCE_LABEL="Numeric Bad",
            COST_STATUS="unavailable",
            collect_session_telemetry=lambda: {
                "source": {"id": "numeric_bad", "label": "Numeric Bad"},
                "totals": {"sessions": 1, "tokens": 10, "cost_usd": 0},
                "sessions": [],
                "daily_models": [{
                    "day": "2026-01-01", "agent": "numeric_bad",
                    "model": "test", "tokens": "ten", "cost": 0,
                }],
                "daily_sessions": [], "daily_tools": [],
            },
        )

        def load(_capability: str, *, agent: str):
            return {"good": good, "numeric_bad": malformed}[agent]

        with mock.patch.object(
            combined_stats, "list_capability_providers",
            return_value=["numeric_bad", "good"],
        ), mock.patch.object(combined_stats, "try_load_capability", side_effect=load):
            data = combined_stats.build_session_telemetry()

        self.assertEqual(data["totals"]["sessions"], 1)
        status = {row["id"]: row for row in data["meta"]["sources"]}
        self.assertFalse(status["numeric_bad"]["available"])
        self.assertEqual(status["numeric_bad"]["message"], "Telemetry source unavailable.")

    def test_codex_is_telemetry_only_not_a_chat_adapter(self) -> None:
        self.assertIn("codex", list_capability_providers("session_telemetry"))
        self.assertNotIn("codex", list_adapters())

    def test_cursor_is_telemetry_only_not_a_chat_adapter(self) -> None:
        self.assertIn("cursor", list_capability_providers("session_telemetry"))
        self.assertNotIn("cursor", list_adapters())

    def test_optional_loader_does_not_hide_transitive_import_failures(self) -> None:
        error = ModuleNotFoundError("No module named 'missing_dependency'")
        error.name = "missing_dependency"
        with mock.patch.object(
            capability_loader.importlib, "import_module", side_effect=error
        ):
            with self.assertRaises(ModuleNotFoundError):
                capability_loader.try_load_capability("session_telemetry", agent="good")


class CursorSessionTelemetryTests(unittest.TestCase):
    def _write_transcript(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_collects_transcript_sessions_with_unclassified_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            transcript = (
                root / "projects" / "work-demo" / "agent-transcripts"
                / session_id / f"{session_id}.jsonl"
            )
            self._write_transcript(transcript, [
                {
                    "role": "user",
                    "message": {
                        "content": [{
                            "type": "text",
                            "text": (
                                "<timestamp>Sunday, Jul 19, 2026, 10:00 AM "
                                "(UTC)</timestamp>\nhello world"
                            ),
                        }],
                    },
                },
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "working"},
                            {"type": "tool_use", "name": "Shell", "input": {}},
                        ],
                    },
                },
            ])

            with mock.patch.dict(os.environ, {"CURSOR_HOME": str(root)}):
                data = cursor_stats.collect_session_telemetry()

            self.assertEqual(data["source"]["id"], "cursor")
            self.assertEqual(data["source"]["cost_status"], "unavailable")
            self.assertEqual(data["totals"]["sessions"], 1)
            session = data["sessions"][0]
            self.assertEqual(session["key"], f"cursor:{session_id}")
            self.assertEqual(session["agent"], "cursor")
            self.assertEqual(session["turns"], 1)
            self.assertGreater(session["tokens"], 0)
            self.assertEqual(session["tokens"], session["unclassified"])
            self.assertFalse(session["breakdown_known"])
            self.assertFalse(session["cost_known"])
            self.assertEqual(session["tools"][0]["name"], "Shell")
            self.assertEqual(data["daily_tools"][0]["name"], "Shell")
            self.assertIn("work-demo", data["project_keys"])

    def test_prefers_native_bubble_tokens_when_state_db_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_data = root / "cursor-user"
            session_id = "11111111-2222-3333-4444-555555555555"
            transcript = (
                root / "projects" / "alpha" / "agent-transcripts"
                / f"{session_id}.jsonl"
            )
            self._write_transcript(transcript, [
                {
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
            ])
            db_path = user_data / "User" / "globalStorage" / "state.vscdb"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(db_path)
            connection.execute(
                "create table cursorDiskKV (key text primary key, value text)"
            )
            connection.execute(
                "insert into cursorDiskKV values (?, ?)",
                (
                    f"composerData:{session_id}",
                    json.dumps({
                        "createdAt": 1_721_390_400_000,
                        "lastUpdatedAt": 1_721_390_760_000,
                        "model": "gpt-test",
                        "workspaceIdentifier": {
                            "uri": {"fsPath": "/work/alpha"},
                        },
                    }),
                ),
            )
            connection.execute(
                "insert into cursorDiskKV values (?, ?)",
                (
                    f"bubbleId:{session_id}:bubble-1",
                    json.dumps({
                        "tokenCount": {"inputTokens": 100, "outputTokens": 20},
                    }),
                ),
            )
            connection.commit()
            connection.close()

            with mock.patch.dict(os.environ, {
                "CURSOR_HOME": str(root),
                "CURSOR_USER_DATA": str(user_data),
            }):
                data = cursor_stats.collect_session_telemetry()

            session = data["sessions"][0]
            self.assertEqual(session["tokens"], 120)
            self.assertEqual(session["fresh"], 100)
            self.assertEqual(session["output"], 20)
            self.assertEqual(session["unclassified"], 0)
            self.assertTrue(session["breakdown_known"])
            self.assertEqual(session["model"], "gpt-test")
            self.assertEqual(session["project_path"], "/work/alpha")


class SessionsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from routers import space as space_route

        self.space_route = space_route
        self.space_route._argus_cache = None
        self.space_route._sessions_build_task = None

    async def test_concurrent_cache_misses_share_one_build(self) -> None:
        calls = 0

        def build() -> dict:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return {"meta": {"sources": []}, "totals": {}, "sessions": []}

        with mock.patch.object(self.space_route, "build_session_telemetry", build):
            first, second = await asyncio.gather(
                self.space_route.sessions_data(), self.space_route.sessions_data()
            )

        self.assertEqual(calls, 1)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(second.headers["cache-control"], "no-store")

    async def test_concurrent_failures_share_one_build_then_retry(self) -> None:
        calls = 0

        def build() -> dict:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            raise RuntimeError("PRIVATE_PATH_SHOULD_NOT_REACH_CLIENT")

        with mock.patch.object(self.space_route, "build_session_telemetry", build):
            failures = await asyncio.gather(
                self.space_route.sessions_data(), self.space_route.sessions_data(),
                return_exceptions=True,
            )
            self.assertEqual(calls, 1)
            self.assertTrue(all(getattr(exc, "status_code", None) == 503 for exc in failures))
            self.assertTrue(all(
                "PRIVATE_PATH" not in str(getattr(exc, "detail", ""))
                for exc in failures
            ))

            # The failed task is retained only for the concurrent wave; the
            # next event-loop turn clears it so a later request can retry.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            retry = await asyncio.gather(
                self.space_route.sessions_data(), return_exceptions=True
            )

        self.assertEqual(calls, 2)
        self.assertEqual(getattr(retry[0], "status_code", None), 503)


if __name__ == "__main__":
    unittest.main()
