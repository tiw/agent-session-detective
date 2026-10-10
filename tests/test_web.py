import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_session_detective.web as web
from agent_session_detective.ir.schema import IR_VERSION
from agent_session_detective.tokenstats import Repeat, TokenStats
from agent_session_detective.web import cache_load, cache_store, discover_sessions, fingerprint

FIXTURES = Path(__file__).parent / "fixtures"


class QoderWebTests(unittest.TestCase):
    def test_discovers_qoder_transcript_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / "demo-workspace" / "transcript.jsonl"
            transcript.parent.mkdir()
            transcript.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual(sessions, [{
                "id": "transcript",
                "path": str(transcript),
                "workspace": "demo-workspace",
                "mtime": transcript.stat().st_mtime,
            }])

    def test_discovers_qoder_transcripts_with_matching_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / "project-one" / "transcript.jsonl"
            second = base / "project-two" / "transcript.jsonl"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual(
                {(session["id"], session["workspace"], session["path"]) for session in sessions},
                {
                    ("transcript", "project-one", str(first)),
                    ("transcript", "project-two", str(second)),
                },
            )

    def test_fingerprint_uses_v10_and_ir_version_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(
                fingerprint(str(transcript), "test-model"),
                "1234.000:5:test-model:v10:ir%s" % IR_VERSION,
            )

    def test_v10_fingerprint_invalidates_v9_cached_results(self):
        # v9 cached billed results lack the turn-growth series rebuilt from
        # the billed prompt rows; the v10 bump must make cache_load miss.
        v9_fp = "1234.000:5:test-model:v9:ir1.6"
        v10_fp = "1234.000:5:test-model:v10:ir1.6"
        result = {"status_line": "stale v9 payload"}

        original_cache_dir = web.CACHE_DIR
        web.CACHE_DIR = Path(tempfile.mkdtemp())
        try:
            cache_store("k", v9_fp, result)
            self.assertEqual(cache_load("k", v9_fp), result)
            self.assertIsNone(cache_load("k", v10_fp))
        finally:
            web.CACHE_DIR = original_cache_dir

    def test_an_ir_version_bump_invalidates_cached_results(self):
        # The motivating bug: IR moved 1.3→1.6 with no manual key bump, so
        # stale cached results (missing the new evidence blocks) kept being
        # served. With IR_VERSION in the key, an IR bump invalidates by
        # itself instead of waiting for a hand bump.
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))
            result = {"status_line": "payload from the previous IR"}

            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                old_fp = fingerprint(str(transcript), "test-model")
                cache_store("k", old_fp, result)
                with patch.object(web, "IR_VERSION", "99.9"):
                    new_fp = fingerprint(str(transcript), "test-model")
                self.assertNotEqual(new_fp, old_fp)
                self.assertIsNone(cache_load("k", new_fp))
            finally:
                web.CACHE_DIR = original_cache_dir


class SessionDiscoveryScopeTests(unittest.TestCase):
    """Which files on disk count as sessions. The qoder projects root holds
    three kinds of jsonl: IDE transcripts at the top level (the auditable
    rich format), terminal-CLI transcripts under <ws>/transcript/ (they load
    degraded — no telemetry), and subagent mirrors under
    <ws>/<session>/subagents/. The first two belong in the sidebar; only the
    subagent mirrors must stay out."""

    def test_does_not_discover_qoder_subagent_files_as_codex_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            rich = base / "demo-workspace" / "11111111-1111-1111-1111-111111111111.jsonl"
            rich.parent.mkdir()
            rich.write_text("{}", encoding="utf-8")
            subagent = rich.with_suffix("") / "subagents" / "agent-aExplore-abc123.jsonl"
            subagent.parent.mkdir(parents=True)
            subagent.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual([s["path"] for s in sessions], [str(rich)])

    def test_discovers_terminal_cli_transcripts(self):
        # <ws>/transcript/<uuid>.jsonl is the terminal-CLI transcript: it
        # loads degraded (no usage telemetry), so it belongs in the sidebar;
        # the workspace shown is the real workspace dir above transcript/.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cli_transcript = (
                base / "demo-workspace" / "transcript" /
                "b1d65022-d35d-4a45-b24f-27eb970e6b86.jsonl")
            cli_transcript.parent.mkdir(parents=True)
            cli_transcript.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual(sessions, [{
                "id": "b1d65022-d35d-4a45-b24f-27eb970e6b86",
                "path": str(cli_transcript),
                "workspace": "demo-workspace",
                "mtime": cli_transcript.stat().st_mtime,
            }])

    def test_discovers_codex_rollout_sessions_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            rollout = base / "2026" / "10" / "09" / "rollout-2026-10-09T03-00-00-abc.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual([s["path"] for s in sessions], [str(rollout)])


class TreeRouteTests(unittest.TestCase):
    """GET /api/tree serves the actual skill tree (IR 1.2 renderer) for one
    session path as a standalone HTML page. No judge and no audit cache: the
    tree derives from the IR document alone."""

    @classmethod
    def setUpClass(cls):
        import threading

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def tree_url(self, session_path: str) -> str:
        return self.base + "/api/tree?path=" + urllib.parse.quote(session_path)

    def test_serves_the_rendered_skill_tree_page(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "tier1.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with urllib.request.urlopen(self.tree_url(str(transcript))) as response:
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])

            self.assertIn("Actual Skill Tree", page)
            self.assertNotIn("<script", page)

    def test_requires_a_path(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/tree")
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_session_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.tree_url("/no/such/session.jsonl"))
        self.assertEqual(ctx.exception.code, 404)

    def test_unparseable_session_is_500_with_error(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "empty.jsonl"
            transcript.write_text("", encoding="utf-8")

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.tree_url(str(transcript)))

        self.assertEqual(ctx.exception.code, 500)
        self.assertIn("error", ctx.exception.read().decode("utf-8"))

    def test_degraded_cli_session_renders_with_a_notice(self):
        # Terminal-CLI transcripts load degraded instead of erroring: the
        # page renders 200 with an honest banner about the missing usage
        # telemetry.
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "cli-session.jsonl"
            records = [
                {"type": "session_meta", "data": {"meta_type": "slash_command"}},
                {"type": "user", "uuid": "u1",
                 "timestamp": "2026-10-09T08:00:00.000Z",
                 "message": {"role": "user",
                             "content": [{"type": "text", "text": "hi"}]}},
            ]
            transcript.write_text(
                "\n".join(json.dumps(r) for r in records), encoding="utf-8")

            with urllib.request.urlopen(self.tree_url(str(transcript))) as response:
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])

            self.assertIn("no usage telemetry", page)

    def test_billed_param_renders_billed_totals_from_the_side_channel(self):
        from agent_session_detective import billing
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / "3b241101-e2bb-4255-8caf-4136c566a962.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            db = base / "local.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE chat_message (session_id TEXT, token_info TEXT)")
            conn.execute("INSERT INTO chat_message VALUES (?, ?)", (
                "3b241101-e2bb-4255-8caf-4136c566a962",
                json.dumps({"prompt_tokens": 1000, "completion_tokens": 40,
                            "cached_tokens": 800})))
            conn.commit()
            conn.close()
            original = billing.DEFAULT_DB_PATH
            billing.DEFAULT_DB_PATH = db
            self.addCleanup(setattr, billing, "DEFAULT_DB_PATH", original)

            with urllib.request.urlopen(
                    self.tree_url(str(transcript)) + "&billed=1") as response:
                page = response.read().decode("utf-8")

            self.assertIn("billed (provider): 1 requests", page)
            self.assertIn("prompt 1000", page)

    def test_billed_param_with_unavailable_side_channel_renders_the_note(self):
        from agent_session_detective import billing
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / "3b241101-e2bb-4255-8caf-4136c566a962.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            original = billing.DEFAULT_DB_PATH
            billing.DEFAULT_DB_PATH = base / "absent.db"
            self.addCleanup(setattr, billing, "DEFAULT_DB_PATH", original)

            with urllib.request.urlopen(
                    self.tree_url(str(transcript)) + "&billed=1") as response:
                page = response.read().decode("utf-8")

            self.assertIn("billed_usage: unavailable", page)

    def test_no_billed_param_renders_no_billed_block(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "tier1.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with urllib.request.urlopen(self.tree_url(str(transcript))) as response:
                page = response.read().decode("utf-8")

            self.assertNotIn("billed (provider)", page)

    def test_ide_db_param_joins_the_chain_from_the_default_db(self):
        import agent_session_detective.billing as billing
        from agent_session_detective import ide_db
        from tests.test_ide_db import (KID_UUID, ROOT_UUID, coded,
                                       identity_decryptor, make_db, row,
                                       write_main_transcript)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (ROOT_UUID + ".jsonl")
            write_main_transcript(transcript)
            db = base / "local.db"
            payload = {"role": "assistant", "content": "hello",
                       "reasoning_content": "", "tool_calls": []}
            make_db(db,
                    children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                    messages=[(KID_UUID, row(1, "assistant", coded(payload)))])
            original = billing.DEFAULT_DB_PATH
            billing.DEFAULT_DB_PATH = db
            self.addCleanup(setattr, billing, "DEFAULT_DB_PATH", original)
            with patch.object(ide_db, "_resolve_decryptor",
                              return_value=identity_decryptor):
                with urllib.request.urlopen(
                        self.tree_url(str(transcript)) + "&ide_db=1") as response:
                    body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("joined via ide-db 1", body)


class TokenstatsSerializationTests(unittest.TestCase):
    def _stats(self):
        repeat = Repeat(
            preview="same output", occurrences=3, tokens_each=100,
            extra_tokens=200, tool_name="Read", chars_each=300,
            occurrence_classes=["first", "post_compaction", "poll"],
            extra_by_class={"post_compaction": 100, "poll": 100})
        return TokenStats(
            repeats=[repeat], repeat_extra_tokens=200,
            repeat_class_totals={"post_compaction": 100, "poll": 100},
            compaction_source="transcript+billed",
            compaction_points=[{"ts": 110.0, "window_start": 100.0,
                                "window_end": 110.0, "pre_prompt": 100000,
                                "post_prompt": 50000}])

    def test_repeats_carry_classification_fields(self):
        payload = web.tokenstats_to_dict(self._stats())
        entry = payload["repeats"][0]
        self.assertEqual(entry["occurrence_classes"],
                         ["first", "post_compaction", "poll"])
        self.assertEqual(entry["extra_by_class"],
                         {"post_compaction": 100, "poll": 100})
        self.assertEqual(entry["tool_name"], "Read")
        self.assertEqual(entry["chars_each"], 300)
        # raw occurrence timestamps stay internal
        self.assertNotIn("occurrence_ts", entry)

    def test_top_level_classification_fields(self):
        payload = web.tokenstats_to_dict(self._stats())
        self.assertEqual(payload["repeat_class_totals"],
                         {"post_compaction": 100, "poll": 100})
        self.assertEqual(payload["compaction_source"], "transcript+billed")
        self.assertEqual(payload["compaction_points"][0]["post_prompt"], 50000)


class CacheKeyTests(unittest.TestCase):
    def test_billed_flag_changes_the_cache_key(self):
        base = {"path": "/tmp/x.jsonl", "expect": None, "steps": None,
                "judge_triggers": False}
        self.assertNotEqual(web.cache_key(base),
                            web.cache_key({**base, "billed": True}))


class RunAuditBilledTests(unittest.TestCase):
    BILLING_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"

    def _sawtooth_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE chat_message "
            "(session_id TEXT, gmt_create INTEGER, token_info TEXT)")
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)", (
            self.BILLING_UUID, 1754709632658,
            json.dumps({"prompt_tokens": 100000, "completion_tokens": 40,
                        "cached_tokens": 80000})))
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)", (
            self.BILLING_UUID, 1754709642658,
            json.dumps({"prompt_tokens": 50000, "completion_tokens": 40,
                        "cached_tokens": 0})))
        conn.commit()
        conn.close()

    def _transcript(self, directory):
        transcript = Path(directory) / (self.BILLING_UUID + ".jsonl")
        transcript.write_text(
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
            '"args": {"path": "a"}}}\n'
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.result", "toolCallId": "c1", "result": '
            '{"output": "hello world"}}}\n',
            encoding="utf-8")
        return transcript

    @staticmethod
    def _iso(epoch_ms):
        stamp = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    def _cli_transcript(self, directory):
        """The b1d65022 shape: terminal-CLI transcript (session_meta
        leader + sessionId records) with no embedded usage telemetry."""
        records = [{
            "type": "session_meta", "sessionId": "s1",
            "timestamp": self._iso(1754709620000), "cwd": "/w",
            "data": {"meta_type": "slash_command"},
        }]
        for i, ts in enumerate((1754709625000, 1754709635000,
                                1754709645000, 1754709655000)):
            records.append({
                "type": "user", "sessionId": "s1",
                "timestamp": self._iso(ts), "cwd": "/w",
                "message": {"role": "user",
                            "content": [{"type": "text",
                                         "text": "request %d" % i}]}})
            records.append({
                "type": "assistant", "sessionId": "s1",
                "timestamp": self._iso(ts + 10000), "cwd": "/w",
                "message": {"role": "assistant",
                            "content": [{"type": "text",
                                         "text": "reply %d" % i}]}})
        transcript = Path(directory) / (self.BILLING_UUID + ".jsonl")
        transcript.write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return transcript

    def _series_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE chat_message "
            "(session_id TEXT, gmt_create INTEGER, token_info TEXT)")
        conn.executemany(
            "INSERT INTO chat_message VALUES (?, ?, ?)",
            [(self.BILLING_UUID, gmt,
              json.dumps({"prompt_tokens": prompt, "completion_tokens": 40,
                          "cached_tokens": cached}))
             for gmt, prompt, cached in (
                 (1754709632658, 100000, 80000),
                 (1754709642658, 50000, 0),
                 (1754709652658, 60000, 0))])
        conn.commit()
        conn.close()

    def test_billed_audit_rebuilds_turn_growth_for_cli_shape_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "local.db"
            self._series_db(db)
            transcript = self._cli_transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                           str(db)):
                    job = web.Job({"path": str(transcript), "billed": True})
                    web.run_audit(job)
                self.assertEqual(job.status, "done")
                stats = job.result["token_stats"]
                rows = stats["turn_growth"]
                self.assertEqual(len(rows), 4)
                self.assertEqual([r["context_at_start"] for r in rows],
                                 [100000, 50000, 60000, 60000])
                self.assertEqual([r["exact"] for r in rows],
                                 [True, True, True, False])
                # The 100000 -> 50000 cold-cache drop sits between turns 1
                # and 2: that bar is compaction-crossed, not negative growth.
                self.assertTrue(rows[0]["crossed_compaction"])
                self.assertFalse(rows[1]["crossed_compaction"])
                self.assertNotEqual(stats["growth_verdict"],
                                    "unavailable (no context telemetry)")
                self.assertTrue(stats["bucket_totals"])
                # Billed rows feed turn contexts only; session-level billed
                # totals stay in document.billing, never in the EST usage.
                self.assertEqual(stats["input_total"], 0)
            finally:
                web.CACHE_DIR = original_cache_dir

    def test_billed_db_unavailable_keeps_growth_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._cli_transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                           str(Path(directory) / "absent.db")):
                    job = web.Job({"path": str(transcript), "billed": True})
                    web.run_audit(job)
                self.assertEqual(job.status, "done")
                stats = job.result["token_stats"]
                self.assertEqual(stats["turn_growth"], [])
                self.assertEqual(stats["growth_verdict"],
                                 "unavailable (no context telemetry)")
                self.assertTrue(any(
                    "billed_compaction unavailable" in s
                    for s in job.steps))
            finally:
                web.CACHE_DIR = original_cache_dir

    def test_billed_audit_classifies_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "local.db"
            self._sawtooth_db(db)
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                           str(db)):
                    job = web.Job({"path": str(transcript), "billed": True})
                    web.run_audit(job)
                self.assertEqual(job.status, "done")
                stats = job.result["token_stats"]
                self.assertEqual(stats["compaction_source"], "billed")
                self.assertEqual(len(stats["compaction_points"]), 1)
                self.assertEqual(stats["compaction_points"][0]["post_prompt"],
                                 50000)
            finally:
                web.CACHE_DIR = original_cache_dir

    def test_audit_without_billed_has_no_compaction_source(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
                self.assertEqual(job.status, "done")
                stats = job.result["token_stats"]
                self.assertIsNone(stats["compaction_source"])
            finally:
                web.CACHE_DIR = original_cache_dir


class FleetClassTotalsTests(unittest.TestCase):
    BILLING_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"

    def test_fleet_payload_carries_repeat_class_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "ws"
            workspace.mkdir()
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl",
                        workspace / (self.BILLING_UUID + ".jsonl"))
            with patch("agent_session_detective.web."
                       "build_token_stats") as fake_build:
                fake_build.return_value = TokenStats(
                    repeat_extra_tokens=300,
                    repeat_class_totals={
                        "post_compaction": 22900, "poll": 100})
                fleet = web.fleet_stats([str(base)])
            self.assertEqual(fleet["repeat_class_totals"],
                             {"post_compaction": 22900, "poll": 100})
            self.assertEqual(fleet["total_repeat_extra_tokens"], 300)


class SuggestNextStepsRepeatTests(unittest.TestCase):
    def _timeline(self):
        return SimpleNamespace(file_reads=[], loads=[])

    def test_with_source_splits_legal_rereads_from_tax(self):
        token_stats = TokenStats(
            repeat_extra_tokens=23000,
            repeat_class_totals={"post_compaction": 22900, "poll": 100},
            compaction_source="billed")
        out = web.suggest_next_steps(self._timeline(), [], [], [], False,
                                     token_stats)
        self.assertTrue(any("合法重读" in line for line in out))
        self.assertTrue(any("真实重读税仅 100" in line for line in out))

    def test_without_source_keeps_the_externalize_message(self):
        token_stats = TokenStats(
            repeat_extra_tokens=23000,
            repeat_class_totals={"unclassified": 23000})
        out = web.suggest_next_steps(self._timeline(), [], [], [], False,
                                     token_stats)
        self.assertTrue(any("状态外置" in line for line in out))


if __name__ == "__main__":
    unittest.main()
