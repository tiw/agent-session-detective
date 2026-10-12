"""Catalog loader contracts that span features."""

import unittest


class DefaultDirsTest(unittest.TestCase):
    def test_qoder_skills_is_a_default_dir(self):
        from agent_session_detective.catalog import DEFAULT_SKILL_DIRS
        self.assertIn("~/.qoder/skills", DEFAULT_SKILL_DIRS)
