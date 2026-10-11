"""The ``catalog-audit`` subcommand, and the dispatch that routes to it.

The dispatch matters on its own: ``session`` is a positional argument, so
without an explicit check ``asd catalog-audit`` is read as a session path named
"catalog-audit" and fails with a confusing "no events parsed" instead of
running an audit.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import cli

FIXTURES = Path(__file__).parent / "fixtures"


def write_skill(root: Path, name: str, description: str = "A real one.",
                body_lines: int = 10) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    body = "\n".join("body line %d" % i for i in range(body_lines))
    (directory / "SKILL.md").write_text(
        "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, description, body),
        encoding="utf-8")
    return directory


# ``--skills-dir`` adds to the built-in dirs rather than replacing them, which
# is right in production and wrong in a test: the developer's own catalog would
# leak in and every count would depend on their machine. Pin the defaults to
# nothing so the fixture dir is the whole catalog.
NO_DEFAULT_DIRS = patch("agent_session_detective.catalog.DEFAULT_SKILL_DIRS", ())


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with NO_DEFAULT_DIRS, redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class DispatchTests(unittest.TestCase):
    def test_subcommand_is_not_read_as_a_session_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code, out, _ = run(["catalog-audit", "--skills-dir", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("catalog:", out)
        self.assertNotIn("No session found", out)

    def test_subcommand_needs_no_session_log(self):
        # The whole point: this runs with no sessions on disk at all, and the
        # session-oriented flags are not part of its surface — it has no
        # session to point them at.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_skill(root, "solo")
            code, out, _ = run(["catalog-audit", "--skills-dir", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("solo", out)

    def test_json_output_is_parsable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_skill(root, "solo")
            code, out, _ = run(["catalog-audit", "--skills-dir", str(root), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["total"], 1)
        self.assertIn("size_tiers", payload)
        self.assertIn("thresholds", payload)

    def test_out_writes_a_file_and_prints_nothing_else(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_skill(root, "solo")
            target = root / "report.txt"
            code, out, _ = run(["catalog-audit", "--skills-dir", str(root),
                                "--out", str(target)])
            written = target.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(target))
        self.assertIn("catalog:", written)
    def test_threshold_overrides_reach_the_measurement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_skill(root, "big", body_lines=600)
            _, out, _ = run(["catalog-audit", "--skills-dir", str(root),
                             "--json", "--max-lines", "1000"])
        payload = json.loads(out)
        self.assertEqual(payload["thresholds"]["max_lines"], 1000)
        self.assertEqual(payload["size_tiers"]["oversize"], 0)

    def test_injected_entries_are_accepted_and_measured(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_skill(root, "scanned")
            truth = root / "truth.json"
            truth.write_text(json.dumps({
                "only-injected": {"description": "Supplied routing text."},
            }), encoding="utf-8")
            _, out, _ = run(["catalog-audit", "--skills-dir", str(root),
                             "--injected", str(truth), "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["total"], 2)
        # No body_path, so it is disclosed as unmeasurable rather than counted
        # as a zero-token skill.
        self.assertEqual(payload["no_body"], ["only-injected"])

    def test_malformed_injected_file_degrades_to_the_scanned_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_skill(root, "scanned")
            truth = root / "bad.json"
            truth.write_text("{not json", encoding="utf-8")
            code, out, err = run(["catalog-audit", "--skills-dir", str(root),
                                  "--injected", str(truth)])
        self.assertEqual(code, 0)
        self.assertIn("catalog: 1 skills", out)
        self.assertIn("No usable entries", err)


class FlatArgumentTests(unittest.TestCase):
    """The session path must keep working exactly as before."""

    def test_session_audit_still_runs_after_adding_a_subcommand(self):
        # --sessions-root narrows only the Kimi root; the Qoder and Codex roots
        # are still searched, so this drives one explicit transcript rather
        # than asserting on whatever sessions the developer has on disk.
        transcript = FIXTURES / "qoder-transcript.jsonl"
        self.assertTrue(transcript.exists())
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "foo.jsonl"
            shutil.copy(transcript, target)
            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>"):
                code, out, _ = run([str(target), "--no-judge"])
        self.assertEqual(code, 0)
        self.assertIn("skill-audit.html", out)

    def test_help_still_works(self):
        with self.assertRaises(SystemExit) as ctx:
            run(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_subcommand_help_documents_its_own_thresholds(self):
        # argparse prints help to the real stdout, so capture it there rather
        # than through run()'s buffer.
        buffer = io.StringIO()
        with NO_DEFAULT_DIRS, redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                cli.main(["catalog-audit", "--help"])
        text = buffer.getvalue()
        self.assertIn("Conventional threshold", text)
        self.assertIn("no session log", text.lower())


if __name__ == "__main__":
    unittest.main()
