"""--tree-out writes the standalone skill-tree page."""

import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import cli

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


if __name__ == "__main__":
    unittest.main()
