import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import agent_session_detective.web as web
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

    def test_fingerprint_uses_v7_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(fingerprint(str(transcript), "test-model"), "1234.000:5:test-model:v7")

    def test_v7_fingerprint_invalidates_v6_cached_results(self):
        # v6 cached results carry the pre-merge skill_loads ledger from
        # IR 1.2; the key bump must make cache_load miss.
        v6_fp = "1234.000:5:test-model:v6"
        v7_fp = "1234.000:5:test-model:v7"
        result = {"status_line": "stale v6 payload"}

        original_cache_dir = web.CACHE_DIR
        web.CACHE_DIR = Path(tempfile.mkdtemp())
        try:
            cache_store("k", v6_fp, result)
            self.assertEqual(cache_load("k", v6_fp), result)
            self.assertIsNone(cache_load("k", v7_fp))
        finally:
            web.CACHE_DIR = original_cache_dir


class SessionDiscoveryScopeTests(unittest.TestCase):
    """Which files on disk count as sessions. The qoder projects root holds
    three kinds of jsonl: IDE transcripts at the top level (the auditable
    rich format), terminal-CLI transcripts under <ws>/transcript/ (no
    telemetry), and subagent mirrors under <ws>/<session>/subagents/. Only
    the first kind belongs in the sidebar — the codex discovery glob must
    not fish the others out of the qoder root."""

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

    def test_does_not_discover_terminal_cli_transcripts(self):
        # <ws>/transcript/<uuid>.jsonl carries no usage telemetry; it stays
        # out of the sidebar until a dedicated adapter exists.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cli_transcript = base / "demo-workspace" / "transcript" / "b1d65022-d35d-4a45-b24f-27eb970e6b86.jsonl"
            cli_transcript.parent.mkdir(parents=True)
            cli_transcript.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual(sessions, [])

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


if __name__ == "__main__":
    unittest.main()
