"""--tree-out writes the standalone skill-tree page."""

import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import cli, ide_db

from tests.test_ide_db import (KID_UUID, ROOT_UUID, coded,
                               identity_decryptor, make_db, row,
                               write_main_transcript)

FIXTURES = Path(__file__).parent / "fixtures"


class TreeOutTest(unittest.TestCase):
    def test_tree_out_writes_a_self_contained_page(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "tier1.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            tree_path = Path(directory) / "tree.html"

            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>"):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    exit_code = cli.main([str(transcript), "--no-judge",
                                          "--tree-out", str(tree_path)])

            self.assertEqual(exit_code, 0)
            page = tree_path.read_text(encoding="utf-8")
            self.assertIn("Actual Skill Tree", page)
            self.assertNotIn("<script", page)
            self.assertIn(str(tree_path), stdout.getvalue())


class TreeOutHintTest(unittest.TestCase):
    def _run(self, argv):
        with patch("agent_session_detective.cli.render_report",
                   return_value="<html>report</html>"):
            stdout = StringIO()
            with redirect_stdout(stdout):
                return cli.main(argv)

    def test_orphan_with_resolvable_children_renders_the_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (ROOT_UUID + ".jsonl")
            write_main_transcript(transcript)
            db = base / "local.db"
            make_db(db, children=[(KID_UUID, "call_00A", "agent_sub_custom")])
            tree_path = base / "tree.html"

            with patch.object(ide_db, "_resolve_decryptor",
                              return_value=identity_decryptor):
                exit_code = self._run(
                    [str(transcript), "--no-judge", "--ide-db-path", str(db),
                     "--tree-out", str(tree_path)])

            self.assertEqual(exit_code, 0)
            page = tree_path.read_text(encoding="utf-8")
            self.assertIn("1 orphan dispatch can be resolved", page)
            self.assertIn("&amp;ide_db=1", page)

    def test_ide_db_flag_attaches_and_suppresses_the_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (ROOT_UUID + ".jsonl")
            write_main_transcript(transcript)
            db = base / "local.db"
            user = {"role": "user",
                    "contents": [{"type": "text", "text": "do the thing"}]}
            make_db(db,
                    children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                    messages=[(KID_UUID, row(1, "user", coded(user),
                                             "req-1"))])
            tree_path = base / "tree.html"

            with patch.object(ide_db, "_resolve_decryptor",
                              return_value=identity_decryptor):
                exit_code = self._run(
                    [str(transcript), "--no-judge", "--ide-db",
                     "--ide-db-path", str(db), "--tree-out", str(tree_path)])

            self.assertEqual(exit_code, 0)
            page = tree_path.read_text(encoding="utf-8")
            self.assertIn("joined via ide-db 1", page)
            self.assertNotIn("can be resolved", page)


if __name__ == "__main__":
    unittest.main()
