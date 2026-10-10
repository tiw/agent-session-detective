import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
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

    def test_fingerprint_uses_v9_and_ir_version_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(
                fingerprint(str(transcript), "test-model"),
                "1234.000:5:test-model:v9:ir%s" % IR_VERSION,
            )

    def test_v9_fingerprint_invalidates_v8_cached_results(self):
        # v8 cached results carry repeats without per-occurrence classes;
        # the v9 bump must make cache_load miss.
        v8_fp = "1234.000:5:test-model:v8:ir1.6"
        v9_fp = "1234.000:5:test-model:v9:ir1.6"
        result = {"status_line": "stale v8 payload"}

        original_cache_dir = web.CACHE_DIR
        web.CACHE_DIR = Path(tempfile.mkdtemp())
        try:
            cache_store("k", v8_fp, result)
            self.assertEqual(cache_load("k", v8_fp), result)
            self.assertIsNone(cache_load("k", v9_fp))
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


if __name__ == "__main__":
    unittest.main()
