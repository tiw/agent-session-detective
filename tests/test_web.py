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


def isolate_uploads(test):
    """Point web.UPLOADS_DIR at a fresh empty dir for one test.

    Discovery merges uploads rows unconditionally, so any test asserting
    exact session listings must not see whatever this machine happens to
    keep in the real uploads directory."""
    original = web.UPLOADS_DIR
    directory = Path(tempfile.mkdtemp())
    web.UPLOADS_DIR = directory
    test.addCleanup(shutil.rmtree, directory, True)
    test.addCleanup(setattr, web, "UPLOADS_DIR", original)


class QoderWebTests(unittest.TestCase):
    def setUp(self):
        isolate_uploads(self)

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

    def test_fingerprint_uses_v12_and_ir_version_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(
                fingerprint(str(transcript), "test-model"),
                "1234.000:5:test-model:v12:ir%s" % IR_VERSION,
            )

    def test_v12_fingerprint_invalidates_v11_cached_results(self):
        # v11 cached results lack the skill_gaps block; the v12 bump must
        # make cache_load miss.
        v11_fp = "1234.000:5:test-model:v11:ir1.6"
        v12_fp = "1234.000:5:test-model:v12:ir1.6"
        result = {"status_line": "stale v11 payload"}

        original_cache_dir = web.CACHE_DIR
        web.CACHE_DIR = Path(tempfile.mkdtemp())
        try:
            cache_store("k", v11_fp, result)
            self.assertEqual(cache_load("k", v11_fp), result)
            self.assertIsNone(cache_load("k", v12_fp))
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

    def setUp(self):
        isolate_uploads(self)

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


def make_flat_sessions(base: Path, count: int, workspace: str = "ws"):
    """``count`` transcripts under ``base/<workspace>/`` with increasing mtimes,
    so index 0 is the oldest and the last is the newest."""
    paths = []
    for i in range(count):
        transcript = base / workspace / ("%02d-session.jsonl" % i)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("{}", encoding="utf-8")
        os.utime(transcript, (1000 + i, 1000 + i))
        paths.append(transcript)
    return paths


class SessionQueryTests(unittest.TestCase):
    """The sidebar shows only the SIDEBAR_LIMIT most recent sessions, while
    discovery finds hundreds. A session that fell out of the top N must still
    be reachable by searching its id or workspace."""

    def setUp(self):
        isolate_uploads(self)

    def test_query_reaches_a_session_beyond_the_sidebar_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, web.SIDEBAR_LIMIT + 2)

            out = web.list_sessions([str(base)], query="00-session")

            self.assertEqual([s["id"] for s in out["sessions"]], ["00-session"])
            self.assertEqual(out["total"], web.SIDEBAR_LIMIT + 2)

    def test_query_matches_the_workspace_name(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, web.SIDEBAR_LIMIT + 2, workspace="paze-test")

            out = web.list_sessions([str(base)], query="paze")

            self.assertEqual(len(out["sessions"]), web.SIDEBAR_LIMIT + 2)

    def test_query_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, 1, workspace="Demo-Work")

            out = web.list_sessions([str(base)], query="DEMO-WORK")

            self.assertEqual([s["id"] for s in out["sessions"]], ["00-session"])

    def test_query_with_no_match_returns_an_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, 3)

            out = web.list_sessions([str(base)], query="no-such-session")

            self.assertEqual(out["sessions"], [])
            self.assertEqual(out["total"], 3)

    def test_empty_query_keeps_the_default_capped_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, web.SIDEBAR_LIMIT + 2)

            out = web.list_sessions([str(base)], query="")

            self.assertEqual(len(out["sessions"]), web.SIDEBAR_LIMIT)
            self.assertNotIn("00-session", [s["id"] for s in out["sessions"]])

    def test_query_caps_at_the_search_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, web.SEARCH_LIMIT + 5, workspace="everywhere")

            out = web.list_sessions([str(base)], query="everywhere")

            self.assertEqual(len(out["sessions"]), web.SEARCH_LIMIT)


class SessionQueryRouteTests(unittest.TestCase):
    """GET /api/sessions?q=... passes the query through to list_sessions."""

    def setUp(self):
        isolate_uploads(self)

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

    def test_q_param_filters_the_sidebar_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, web.SIDEBAR_LIMIT + 2)

            with patch.object(web, "DEFAULT_ROOTS", [str(base)]):
                with urllib.request.urlopen(self.base + "/api/sessions?q=00-session") as r:
                    payload = json.loads(r.read().decode("utf-8"))

            self.assertEqual([s["id"] for s in payload["sessions"]], ["00-session"])

    def test_no_q_param_returns_the_capped_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            make_flat_sessions(base, web.SIDEBAR_LIMIT + 2)

            with patch.object(web, "DEFAULT_ROOTS", [str(base)]):
                with urllib.request.urlopen(self.base + "/api/sessions") as r:
                    payload = json.loads(r.read().decode("utf-8"))

            self.assertEqual(len(payload["sessions"]), web.SIDEBAR_LIMIT)


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

    def test_scanned_skill_dirs_change_the_cache_key(self):
        # The catalog is part of the result, so two runs differing only in
        # which directories were scanned must not share a cache entry.
        base = {"path": "/tmp/x.jsonl", "expect": None, "steps": None,
                "judge_triggers": False}
        self.assertNotEqual(
            web.cache_key(base),
            web.cache_key({**base, "skills_dirs": ["/tmp/skills"]}))

    def test_identical_skill_dirs_reuse_the_same_cache_key(self):
        base = {"path": "/tmp/x.jsonl", "skills_dirs": ["/tmp/a", "/tmp/b"]}
        same = {"path": "/tmp/x.jsonl", "skills_dirs": ["/tmp/b", "/tmp/a"]}
        self.assertEqual(web.cache_key(base), web.cache_key(same))


class ScanSkillDirsTests(unittest.TestCase):
    """The audit form previews what each typed directory contributes, so a
    typo is distinguishable from a directory that simply has no skills."""

    def _skill(self, root: Path, name: str, description: str = "A real one."):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: %s\ndescription: %s\n---\nBody.\n" % (name, description),
            encoding="utf-8")
        return d

    def test_counts_flat_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._skill(base, "alpha")
            self._skill(base, "beta")

            out = web.scan_skill_dirs([str(base)])

            self.assertEqual(out["skills"], 2)
            self.assertTrue(out["dirs"][0]["exists"])
            self.assertEqual(out["dirs"][0]["parse_failures"], 0)

    def test_finds_skills_nested_like_a_plugin_cache(self):
        # Plugin caches keep skills at <vendor>/<plugin>/<version>/SKILL.md;
        # a flat glob would report 0 and look like a broken path.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._skill(base / "vendor" / "plugin" / "1.0.0", "nested")

            out = web.scan_skill_dirs([str(base)])

            self.assertEqual(out["skills"], 1)
            self.assertEqual(out["dirs"][0]["names"], ["nested"])

    def test_versioned_duplicates_collapse_to_one_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._skill(base / "vendor" / "plugin" / "1.0.0", "duped")
            self._skill(base / "vendor" / "plugin" / "2.0.0", "duped")

            out = web.scan_skill_dirs([str(base)])

            self.assertEqual(out["skills"], 1)

    def test_missing_path_is_reported_as_absent_not_empty(self):
        out = web.scan_skill_dirs(["/no/such/dir"])

        self.assertFalse(out["dirs"][0]["exists"])
        self.assertEqual(out["dirs"][0]["skills"], 0)

    def test_blank_entries_are_skipped(self):
        out = web.scan_skill_dirs(["", "   "])
        self.assertEqual(out["dirs"], [])

    def test_unparsable_description_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._skill(base, "nodesc", description="")

            out = web.scan_skill_dirs([str(base)])

            self.assertEqual(out["dirs"][0]["parse_failures"], 1)


class InjectedSkillTextTests(unittest.TestCase):
    """The web form pastes skill text inline rather than naming a file, so the
    audit path has to accept the JSON as a string."""

    INLINE = ('{"cloud-service": {"description": "Sites need a backend.", '
              '"intent": "接入托管后端"}}')

    def test_inline_json_string_is_parsed(self):
        skills = web._injected_for({"injected": self.INLINE})
        self.assertEqual([s.name for s in skills], ["cloud-service"])
        self.assertEqual(skills[0].intent, "接入托管后端")
        self.assertTrue(skills[0].is_injected)

    def test_inline_object_is_accepted_too(self):
        skills = web._injected_for({"injected": {"a": {"description": "A."}}})
        self.assertEqual([s.name for s in skills], ["a"])

    def test_malformed_inline_json_degrades_to_nothing(self):
        # A typo in the pasted text must not take the whole audit down.
        self.assertEqual(web._injected_for({"injected": "{bad json"}), [])

    def test_absent_injection_is_empty(self):
        self.assertEqual(web._injected_for({}), [])
        self.assertEqual(web._injected_for({"injected": "  "}), [])

    def test_path_form_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truth.json"
            path.write_text(self.INLINE, encoding="utf-8")
            skills = web._injected_for({"injected_path": str(path)})
        self.assertEqual([s.name for s in skills], ["cloud-service"])


class InjectedCacheKeyTests(unittest.TestCase):
    """Injected text becomes catalog content, so it has to separate cache
    entries — but cosmetic reformatting of the same payload should not."""

    BASE = {"path": "/tmp/x.jsonl"}
    A = '{"a": {"description": "One."}}'
    REFORMATTED = '{\n  "a": {\n    "description": "One."\n  }\n}'

    def test_injection_changes_the_cache_key(self):
        self.assertNotEqual(web.cache_key(self.BASE),
                            web.cache_key({**self.BASE, "injected": self.A}))

    def test_different_content_changes_the_cache_key(self):
        self.assertNotEqual(
            web.cache_key({**self.BASE, "injected": self.A}),
            web.cache_key({**self.BASE, "injected": '{"a": {"description": "Two."}}'}))

    def test_reformatting_the_same_payload_keeps_the_cache_key(self):
        self.assertEqual(
            web.cache_key({**self.BASE, "injected": self.A}),
            web.cache_key({**self.BASE, "injected": self.REFORMATTED}))

    def test_entry_order_keeps_the_cache_key(self):
        two = '{"a": {"description": "A."}, "b": {"description": "B."}}'
        reordered = '{"b": {"description": "B."}, "a": {"description": "A."}}'
        self.assertEqual(
            web.cache_key({**self.BASE, "injected": two}),
            web.cache_key({**self.BASE, "injected": reordered}))


class RunAuditInjectedTests(unittest.TestCase):
    def _transcript(self, directory):
        transcript = Path(directory) / "s.jsonl"
        transcript.write_text(
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
            '"args": {"path": "a"}}}\n'
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.result", "toolCallId": "c1", "result": '
            '{"output": "hello world"}}}\n',
            encoding="utf-8")
        return transcript

    def test_audit_merges_injected_skills_and_reports_the_count(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({
                    "path": str(transcript),
                    "injected": '{"cloud-service": {"description": "Backend."}}',
                })
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            self.assertEqual(job.result["injected_count"], 1)
            self.assertEqual(job.result["catalog_size"],
                             len(web.load_catalog()) + 1)

    def test_audit_without_injection_reports_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            self.assertEqual(job.result["injected_count"], 0)


class ScanDirsRouteTests(unittest.TestCase):
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

    def _post(self, payload):
        request = urllib.request.Request(
            self.base + "/api/scan-dirs",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_returns_a_row_per_directory(self):
        payload = self._post({"dirs": ["/no/such/a", "/no/such/b"]})
        self.assertEqual(len(payload["dirs"]), 2)
        self.assertFalse(payload["dirs"][0]["exists"])

    def test_non_list_dirs_degrades_to_empty(self):
        payload = self._post({"dirs": "not-a-list"})
        self.assertEqual(payload["dirs"], [])


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

    def setUp(self):
        isolate_uploads(self)

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


class RunAuditBillTests(unittest.TestCase):
    def test_run_audit_payload_carries_the_bill_and_returns_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / (
                "3b241101-e2bb-4255-8caf-4136c566a962.jsonl")
            transcript.write_text(
                '{"type": "context.append_loop_event", "event": '
                '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
                '"args": {"path": "a"}}}\n'
                '{"type": "context.append_loop_event", "event": '
                '{"type": "tool.result", "toolCallId": "c1", "result": '
                '{"output": "hello world"}}}\n',
                encoding="utf-8")

            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            bill = job.result["bill"]
            self.assertEqual(sorted(bill),
                             ["billed", "cost_rows", "est_subtotal",
                              "notes", "routing"])
            keys = [row["key"] for row in bill["cost_rows"]]
            # the minimal transcript carries no usage telemetry, so the
            # bucket-derived rows are omitted per the no-volume rule and
            # only the two unconditional rows remain
            self.assertEqual(keys, ["output", "cache_signal"])
            self.assertEqual(job.result["subagent_returns"]["rows"], [])
            self.assertTrue(
                any("账单通道未接入" in note for note in bill["notes"]))


class CatalogAuditEndpointTests(unittest.TestCase):
    """The web panel runs the same measurement as ``asd catalog-audit``.

    Static, offline, and independent of any session: it needs no log, no
    judge, and no credentials, and it makes no claim about agent behaviour.
    """

    def _skill(self, root: Path, name: str, description: str = "A real one.",
               body_lines: int = 10) -> None:
        d = root / name
        d.mkdir(parents=True)
        body = "\n".join("body line %d" % i for i in range(body_lines))
        (d / "SKILL.md").write_text(
            "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, description, body),
            encoding="utf-8")

    def test_returns_all_four_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._skill(base, "alpha", "x" * 400, body_lines=900)
            with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
                out = web.run_catalog_audit({"skills_dirs": [str(base)]})
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["size_tiers"]["oversize"], 1)
        self.assertEqual(out["desc_tiers"]["verbose"], 1)
        self.assertEqual(out["parse_failures"], [])
        self.assertIsNotNone(out["ratio"])

    def test_runs_with_no_session_no_judge_and_no_params(self):
        with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
            out = web.run_catalog_audit({})
        self.assertIn("total", out)
        self.assertIn("thresholds", out)
        self.assertIn("estimator", out)

    def test_threshold_overrides_from_the_request_are_honoured(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._skill(base, "alpha", body_lines=900)
            with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
                out = web.run_catalog_audit({"skills_dirs": [str(base)],
                                             "max_lines": 5000})
        self.assertEqual(out["thresholds"]["max_lines"], 5000)
        self.assertEqual(out["size_tiers"]["oversize"], 0)

    def test_absurd_threshold_falls_back_instead_of_grading_everything(self):
        # A zero/negative threshold would mark every skill a defect and read
        # as a real finding, so it is treated as a bad request.
        with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
            out = web.run_catalog_audit({"max_lines": 0})
        self.assertEqual(out["thresholds"]["max_lines"], 500)

    def test_non_numeric_threshold_falls_back(self):
        with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
            out = web.run_catalog_audit({"max_desc_chars": "abc"})
        self.assertEqual(out["thresholds"]["max_desc_chars"], 300)

    def test_injected_entry_without_body_is_disclosed_not_counted_as_zero(self):
        injected = json.dumps({"ghost": {"description": "Supplied routing text."}})
        with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
            out = web.run_catalog_audit({"injected": injected})
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["no_body"], ["ghost"])
        self.assertEqual(out["body_count"], 0)
        self.assertEqual(out["injected_count"], 1)

    def test_malformed_injected_payload_still_returns_an_audit(self):
        with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
            out = web.run_catalog_audit({"injected": "{not json"})
        self.assertEqual(out["injected_count"], 0)
        self.assertIn("total", out)

    def test_lists_are_capped_for_the_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for i in range(30):
                self._skill(base, "s%02d" % i)
            with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ()):
                out = web.run_catalog_audit({"skills_dirs": [str(base)]})
        self.assertEqual(out["total"], 30)
        self.assertEqual(len(out["top"]), 20)

    def test_endpoint_rejects_a_non_object_payload(self):
        with self.assertRaises(Exception):
            web.run_catalog_audit(["not", "a", "dict"])


class CatalogAuditPanelRenderTests(unittest.TestCase):
    """The panel renders the duplication and closure blocks.

    app.js is a plain browser script with no module system, so the
    regression net pins the payload wiring itself — the same source-level
    approach the lifecycle and tree contracts use in test_wire. Every
    asserted string is a payload read the section cannot render without.
    """

    def _app_js(self) -> str:
        return (
            Path(web.__file__).parent / "webapp" / "app.js"
        ).read_text(encoding="utf-8")

    def test_duplication_section_reads_its_payload_block(self):
        app_js = self._app_js()
        self.assertIn("重复度", app_js)
        self.assertIn("d.duplicates", app_js)
        self.assertIn("d.dup_rate", app_js)
        self.assertIn("waste_tokens_est", app_js)

    def test_duplication_section_discloses_the_convention_thresholds(self):
        app_js = self._app_js()
        self.assertIn("d.thresholds.dup_containment", app_js)
        self.assertIn("d.thresholds.desc_dup_containment", app_js)

    def test_closure_section_reads_its_payload_block(self):
        app_js = self._app_js()
        self.assertIn("加载闭包", app_js)
        self.assertIn("d.closures", app_js)
        self.assertIn("d.thresholds.closure_warn_tokens", app_js)
        self.assertIn("d.thresholds.closure_depth", app_js)

    def test_closure_section_keeps_unresolved_and_truncation_visible(self):
        app_js = self._app_js()
        self.assertIn("未解析", app_js)
        self.assertIn("深度截断", app_js)


class AuditLibraryBlockTests(unittest.TestCase):
    """The audit result carries the declared-vs-observed library block.

    The diff can only be as real as the catalog the run loaded, so the
    payload must include the identity join and the three-bucket diff —
    raw edges alone leave the feedback loop blind.
    """

    def _transcript(self, directory):
        transcript = Path(directory) / "s.jsonl"
        transcript.write_text(
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
            '"args": {"path": "a"}}}\n'
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.result", "toolCallId": "c1", "result": '
            '{"output": "hello world"}}}\n',
            encoding="utf-8")
        return transcript

    def test_audit_payload_carries_the_library_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._transcript(directory)
            skills = Path(directory) / "skills"
            (skills / "alpha").mkdir(parents=True)
            (skills / "alpha" / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: Alpha skill.\n"
                "requires:\n  - beta\n---\n# Alpha\n", encoding="utf-8")
            (skills / "beta").mkdir()
            (skills / "beta" / "SKILL.md").write_text(
                "---\nname: beta\ndescription: Beta skill.\n---\n# Beta\n",
                encoding="utf-8")
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                with patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS",
                           ()):
                    job = web.Job({
                        "path": str(transcript),
                        "skills_dirs": [str(skills)],
                    })
                    web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            library = job.result["library"]
            self.assertEqual(set(library), {"edges", "join", "diff"})
            self.assertEqual(library["diff"]["over_declared"],
                             [{"a": "alpha", "b": "beta"}])
            self.assertEqual(library["join"]["ambiguous"], [])


class AuditLibraryRenderTests(unittest.TestCase):
    """The session audit renders the declared-vs-observed section.

    Source-level pinning, same rationale as the catalog panel contract:
    every asserted string is a payload read the section cannot render
    without.
    """

    def _app_js(self) -> str:
        return (
            Path(web.__file__).parent / "webapp" / "app.js"
        ).read_text(encoding="utf-8")

    def test_library_section_reads_its_payload_block(self):
        app_js = self._app_js()
        self.assertIn("声明 vs 观测", app_js)
        self.assertIn("r.library", app_js)
        self.assertIn("lib.diff", app_js)
        self.assertIn("over_declared", app_js)
        self.assertIn("implicit", app_js)
        self.assertIn("consistent", app_js)
        self.assertIn("implicit_min_weight", app_js)

    def test_library_section_keeps_the_join_footnote(self):
        app_js = self._app_js()
        self.assertIn("lib.join", app_js)
        self.assertIn("ambiguous", app_js)
        self.assertIn("renamed", app_js)

    def test_library_section_has_a_help_entry(self):
        app_js = self._app_js()
        self.assertIn('"library":', app_js)
        self.assertIn('helpDot("library")', app_js)


class InjectedDisclosurePayloadTests(unittest.TestCase):
    """The result payload has to name the supplied skills.

    The web UI marks them from this list, so an entry that lost its
    ``source`` or ``intent`` here would render as ordinary catalog data.
    """

    def test_payload_names_every_supplied_skill(self):
        payload = web.injected_skills_payload({"injected": json.dumps({
            "library": {"description": "Supplied routing text.",
                        "intent": "资料库统一入口"},
            "cloud-service": {"description": "Backend for sites."},
        })})
        self.assertEqual(sorted(s["name"] for s in payload),
                         ["cloud-service", "library"])
        by_name = {s["name"]: s for s in payload}
        self.assertEqual(by_name["library"]["intent"], "资料库统一入口")
        self.assertIsNone(by_name["cloud-service"]["intent"])
        for entry in payload:
            self.assertEqual(entry["source"], "<injected>")

    def test_payload_follows_the_injected_path_route_too(self):
        # Reading the payload from a different source than the audit merged
        # would mark a skill the audit never saw, or miss one it did.
        with tempfile.TemporaryDirectory() as directory:
            truth = Path(directory) / "truth.json"
            truth.write_text(json.dumps({
                "library": {"description": "From a file."}}), "utf-8")
            payload = web.injected_skills_payload(
                {"injected_path": str(truth)})
        self.assertEqual([s["name"] for s in payload], ["library"])

    def test_malformed_payload_yields_nothing_rather_than_raising(self):
        self.assertEqual(
            web.injected_skills_payload({"injected": "{not json"}), [])

    def test_absent_payload_yields_nothing(self):
        self.assertEqual(web.injected_skills_payload({}), [])


def qoder_transcript_text():
    record = {
        "type": "user", "timestamp": "2026-10-10T08:00:00.000Z",
        "uuid": "u1", "parentUuid": None, "isSidechain": False,
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    return json.dumps(record) + "\n"


class UploadStoreTests(unittest.TestCase):
    def setUp(self):
        self._orig = web.UPLOADS_DIR
        web.UPLOADS_DIR = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, web.UPLOADS_DIR, True)
        self.addCleanup(setattr, web, "UPLOADS_DIR", self._orig)

    def test_store_upload_persists_file_and_manifest(self):
        result = web.store_upload(
            qoder_transcript_text().encode("utf-8"),
            "53e08e97-70ce-4aff-8647-c49b4f408d83-木晓-20261010.jsonl")

        session = result["session"]
        self.assertFalse(result["dedup"])
        self.assertTrue(session["uploaded"])
        self.assertEqual(session["workspace"], "upload")
        self.assertEqual(session["id"],
                         "53e08e97-70ce-4aff-8647-c49b4f408d83-木晓-20261010")
        stored = Path(session["path"])
        self.assertEqual(stored.parent, web.UPLOADS_DIR)
        self.assertTrue(stored.is_file())
        self.assertIn(stored.name, web.load_upload_manifest())

    def test_same_content_dedupes_to_one_file(self):
        body = qoder_transcript_text().encode("utf-8")
        first = web.store_upload(body, "one.jsonl")
        second = web.store_upload(body, "two.jsonl")

        self.assertFalse(first["dedup"])
        self.assertTrue(second["dedup"])
        self.assertEqual(first["session"]["path"], second["session"]["path"])
        self.assertEqual(len(list(web.UPLOADS_DIR.glob("*.jsonl"))), 1)

    def test_upload_name_never_escapes_the_uploads_dir(self):
        result = web.store_upload(
            qoder_transcript_text().encode("utf-8"), "../../evil.jsonl")

        stored = Path(result["session"]["path"])
        self.assertEqual(stored.parent, web.UPLOADS_DIR)
        self.assertEqual(stored.name, "evil.jsonl")
        self.assertEqual(
            list(p.name for p in web.UPLOADS_DIR.glob("*.jsonl")), ["evil.jsonl"])

    def test_unparseable_payload_is_rejected_and_leaves_no_file(self):
        with self.assertRaises(web.UploadRejected):
            web.store_upload(b"not a session at all", "bad.jsonl")

        self.assertEqual(list(web.UPLOADS_DIR.glob("*.jsonl")), [])

    def test_delete_upload_removes_file_and_manifest_entry(self):
        result = web.store_upload(
            qoder_transcript_text().encode("utf-8"), "gone.jsonl")
        stored = Path(result["session"]["path"])

        self.assertEqual(web.delete_upload(str(stored)), "gone.jsonl")
        self.assertFalse(stored.exists())
        self.assertNotIn("gone.jsonl", web.load_upload_manifest())

    def test_delete_upload_refuses_paths_outside_the_uploads_dir(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "rollout-x.jsonl"
            outside.write_text("{}", encoding="utf-8")

            self.assertIsNone(web.delete_upload(str(outside)))
            self.assertTrue(outside.exists())


class UploadRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading

        cls._orig = web.UPLOADS_DIR
        web.UPLOADS_DIR = Path(tempfile.mkdtemp())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(web.UPLOADS_DIR, True)
        web.UPLOADS_DIR = cls._orig

    def test_upload_round_trips_into_the_session_listing(self):
        request = urllib.request.Request(
            self.base + "/api/upload?name=" + urllib.parse.quote(
                "53e08e97-70ce-4aff-8647-c49b4f408d83-x.jsonl"),
            data=qoder_transcript_text().encode("utf-8"), method="POST")
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertFalse(payload["dedup"])
        with patch.object(web, "DEFAULT_ROOTS", []):
            sessions = web.discover_sessions()
        entry = [s for s in sessions if s["path"] == payload["session"]["path"]]
        self.assertEqual(len(entry), 1)
        self.assertTrue(entry[0]["uploaded"])

    def test_garbage_upload_is_rejected_with_400(self):
        request = urllib.request.Request(
            self.base + "/api/upload?name=bad.jsonl",
            data=b"garbage", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(list(web.UPLOADS_DIR.glob("*.jsonl")), [])

    def test_oversized_upload_is_rejected_with_413(self):
        with patch.object(web, "MAX_UPLOAD_BYTES", 8):
            request = urllib.request.Request(
                self.base + "/api/upload?name=big.jsonl",
                data=qoder_transcript_text().encode("utf-8"), method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 413)
        self.assertEqual(list(web.UPLOADS_DIR.glob("*.jsonl")), [])

    def test_delete_removes_an_uploaded_session(self):
        request = urllib.request.Request(
            self.base + "/api/upload?name=del.jsonl",
            data=qoder_transcript_text().encode("utf-8"), method="POST")
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        delete = urllib.request.Request(
            self.base + "/api/upload?path=" + urllib.parse.quote(
                payload["session"]["path"]), method="DELETE")
        with urllib.request.urlopen(delete) as response:
            self.assertEqual(
                json.loads(response.read().decode("utf-8"))["deleted"], "del.jsonl")
        self.assertFalse(Path(payload["session"]["path"]).exists())

    def test_delete_refuses_foreign_paths(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "rollout-x.jsonl"
            outside.write_text("{}", encoding="utf-8")
            request = urllib.request.Request(
                self.base + "/api/upload?path=" + urllib.parse.quote(str(outside)),
                method="DELETE")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request)
            self.assertEqual(ctx.exception.code, 404)
            self.assertTrue(outside.exists())


class UploadDiscoveryTests(unittest.TestCase):
    def test_uploaded_sessions_join_the_discovery_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            scanned = Path(directory) / "ws" / "transcript.jsonl"
            scanned.parent.mkdir()
            scanned.write_text("{}", encoding="utf-8")
            uploads = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, uploads, True)
            (uploads / "53e08e97-export.jsonl").write_text(
                qoder_transcript_text(), encoding="utf-8")

            with patch.object(web, "UPLOADS_DIR", uploads):
                sessions = web.discover_sessions([str(Path(directory))])

            by_path = {s["path"]: s for s in sessions}
            self.assertTrue(by_path[str(scanned)].get("uploaded") is not True)
            uploaded = by_path[str(uploads / "53e08e97-export.jsonl")]
            self.assertTrue(uploaded["uploaded"])
            self.assertEqual(uploaded["workspace"], "upload")


class RunAuditSkillGapTests(unittest.TestCase):
    def _transcript(self, directory):
        transcript = Path(directory) / "s.jsonl"
        transcript.write_text(
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
            '"args": {"path": "a"}}}\n'
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.result", "toolCallId": "c1", "result": '
            '{"output": "hello world"}}}\n',
            encoding="utf-8")
        return transcript

    def test_result_carries_skill_gap_block(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            gap = job.result["skill_gaps"]
            self.assertEqual(
                sorted(gap),
                ["join", "listed_unloaded", "loaded_unused", "missed_triggers",
                 "notes", "sets", "unjudged"])
            self.assertIsInstance(gap["join"]["matched"], int)


PIPELINE_LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-10-12T08:00:01.000Z",
     "message": {"content": [{"type": "text", "text": "run the change"}]}},
    {"type": "attachment", "timestamp": "2026-10-12T08:00:02.000Z",
     "attachment": {"type": "skill_listing",
                    "content": "- alpha: Use when testing.\n"
                               "- beta: Use when billing.\n"}},
    {"type": "assistant", "uuid": "a1",
     "timestamp": "2026-10-12T08:00:03.000Z",
     "message": {"content": [
         {"type": "tool_use", "id": "t1", "name": "Skill",
          "input": {"skill": "alpha"}}]}},
    {"type": "user", "uuid": "r1", "timestamp": "2026-10-12T08:00:04.000Z",
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                              "content": "Launching skill: alpha"}]}},
    {"type": "attachment", "timestamp": "2026-10-12T08:00:05.000Z",
     "attachment": {"type": "invoked_skills",
                    "skills": [{"name": "alpha",
                                "content": "alpha body text here"}]}},
    {"type": "assistant", "uuid": "a2",
     "timestamp": "2026-10-12T08:00:06.000Z",
     "message": {"content": [
         {"type": "tool_use", "id": "t2", "name": "Read",
          "input": {"file_path": "/ws/.changes/demo/progress.md"}}]}},
    {"type": "user", "uuid": "r2", "timestamp": "2026-10-12T08:00:07.000Z",
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                              "content": "current step 3"}]}},
    {"type": "attachment", "timestamp": "2026-10-12T08:00:08.000Z",
     "attachment": {"type": "skill_listing",
                    "content": "- alpha: Use when testing.\n"
                               "- beta: Use when billing.\n"}},
]

DIRECT_LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-10-12T08:00:01.000Z",
     "message": {"content": [{"type": "text", "text": "quick question"}]}},
    {"type": "assistant", "uuid": "a1",
     "timestamp": "2026-10-12T08:00:02.000Z",
     "message": {"content": [{"type": "text", "text": "answer"}]}},
]


class RunAuditProcessTests(unittest.TestCase):
    def setUp(self):
        isolate_uploads(self)

    def test_result_carries_process_block_and_ceremony_row(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "s.jsonl"
            transcript.write_text(
                "\n".join(json.dumps(line) for line in PIPELINE_LINES),
                encoding="utf-8")
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            process = job.result["process"]
            self.assertEqual(process["style"], "pipeline")
            self.assertEqual(process["workflow"], {"reads": 1, "writes": 0})
            # two listing attachments, each offering both skills
            self.assertEqual(process["listing"]["occurrences"], 4)
            self.assertEqual(process["listing"]["reinject_occurrences"], 2)
            self.assertGreater(
                process["listing"]["reinject_tokens_est"], 0)
            self.assertEqual(
                sorted(process),
                ["inject", "interventions", "listing", "notes",
                 "skill_loads", "style", "workflow"])
            keys = {row["key"] for row in job.result["bill"]["cost_rows"]}
            self.assertIn("ceremony", keys)


class FleetProcessGroupsTests(unittest.TestCase):
    def setUp(self):
        isolate_uploads(self)

    def test_sessions_group_by_style_with_ceremony_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "ws"
            workspace.mkdir()
            (workspace / ("a" * 8 + "-1111-1111-1111-111111111111.jsonl")
             ).write_text(
                "\n".join(json.dumps(line) for line in PIPELINE_LINES),
                encoding="utf-8")
            (workspace / ("b" * 8 + "-2222-2222-2222-222222222222.jsonl")
             ).write_text(
                "\n".join(json.dumps(line) for line in DIRECT_LINES),
                encoding="utf-8")
            fleet = web.fleet_stats([str(base)])

        groups = fleet["process_groups"]
        self.assertEqual(sorted(groups), ["direct", "pipeline"])
        self.assertEqual(groups["pipeline"]["sessions"], 1)
        self.assertEqual(groups["direct"]["sessions"], 1)
        self.assertGreater(
            groups["pipeline"]["total_listing_reinject_tokens_est"], 0)
        self.assertEqual(
            groups["direct"]["total_listing_reinject_tokens_est"], 0)
        self.assertGreaterEqual(groups["pipeline"]["avg_turns"], 1)


if __name__ == "__main__":
    unittest.main()
