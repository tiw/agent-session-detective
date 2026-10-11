"""Externally injected skill text: load, merge, and provenance.

Injected entries exist so the audit can reason about skills the scanned
catalog misses or mis-parses. Two rules are load-bearing here:

1. an injected entry REPLACES a same-named catalog entry (that is the point
   of supplying text — to correct the parser, not to sit beside it), and
2. injected text stays labelled, because it is supplied input and must not
   be reported as something the log recorded.
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_detective.catalog import (
    Skill,
    _parse_frontmatter,
    load_injected,
    load_skill_file,
    merge_catalog,
    parse_failures,
)


def write(tmp: Path, name: str, text: str) -> Path:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d / "SKILL.md"


class ParseFailureTest(unittest.TestCase):
    def test_block_scalar_description_is_parsed_not_flagged(self):
        # A block scalar is valid YAML, not a defect. The folded text is the
        # routing wording, so it must survive parsing and must NOT be flagged
        # as a failure — flagging it would report a nonexistent problem.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = write(tmp, "blocky", "---\nname: blocky\ndescription: >\n"
                                     "  Real routing text here.\n---\nBody.\n")
            skill = load_skill_file(path)
        self.assertFalse(skill.description_parse_failed)
        self.assertEqual(skill.description, "Real routing text here.")
        self.assertNotIn(skill, parse_failures([skill]))

    def test_strip_and_keep_block_scalars_are_both_understood(self):
        # ">-" strips the trailing newline; "|" keeps newlines. Both are real
        # descriptions, and neither may leak its indicator into the value.
        for header in (">-", ">-", "|", "|-"):
            with tempfile.TemporaryDirectory() as td:
                path = write(Path(td), "s", "---\nname: s\ndescription: %s\n"
                                       "  First line.\n  Second line.\n---\nBody.\n"
                                       % header)
                skill = load_skill_file(path)
            self.assertNotIn(">", skill.description)
            self.assertNotIn("|", skill.description)
            self.assertIn("First line.", skill.description)
            self.assertIn("Second line.", skill.description)
            self.assertFalse(skill.description_parse_failed)

    def test_indented_multiline_description_is_joined(self):
        # "description:" followed by indented prose is a plain multi-line
        # scalar. Dropping those lines would leave no routing wording at all.
        with tempfile.TemporaryDirectory() as td:
            path = write(Path(td), "w", "---\nname: w\ndescription:\n"
                                   "  All network access goes here.\n"
                                   "  Use when the user asks to search.\n---\nBody.\n")
            skill = load_skill_file(path)
        self.assertFalse(skill.description_parse_failed)
        self.assertIn("All network access goes here.", skill.description)
        self.assertIn("Use when the user asks to search.", skill.description)

    def test_nested_mapping_does_not_leak_into_top_level(self):
        # "metadata:" with indented children is a nested map. A line scanner
        # lifts "author"/"version" to the top level, where they are invisible
        # to a reader and free to collide with real keys — so the shape of the
        # parsed dict is asserted, not just the surviving description.
        text = ("---\nname: m\n"
                "description: Real routing text.\n"
                "metadata:\n  author: someone\n  version: \"1.2\"\n"
                "---\nBody.\n")
        parsed = _parse_frontmatter(text)
        self.assertNotIn("author", parsed)
        self.assertNotIn("version", parsed)
        self.assertEqual(parsed["metadata"], {"author": "someone", "version": "1.2"})
        with tempfile.TemporaryDirectory() as td:
            skill = load_skill_file(write(Path(td), "m", text))
        self.assertEqual(skill.description, "Real routing text.")

    def test_nested_child_cannot_overwrite_a_real_key(self):
        # The failure mode that actually corrupted data: a nested child named
        # like a real key landed at top level and replaced its value.
        parsed = _parse_frontmatter(
            "---\nname: real\ndescription: The true description.\n"
            "metadata:\n  description: The wrong one.\n---\nB\n"
        )
        self.assertEqual(parsed["description"], "The true description.")

    def test_normal_description_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = write(tmp, "plain", "---\nname: plain\n"
                                    "description: Use when auditing logs.\n---\nBody.\n")
            skill = load_skill_file(path)
        self.assertFalse(skill.description_parse_failed)

    def test_missing_frontmatter_is_a_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = write(tmp, "bare", "No frontmatter at all.\n")
            skill = load_skill_file(path)
        self.assertTrue(skill.description_parse_failed)
        self.assertIn(skill, parse_failures([skill]))

    def test_frontmatter_without_description_is_a_failure(self):
        # The file has frontmatter but no routing wording: still a failure,
        # because the judge would have nothing to route on.
        with tempfile.TemporaryDirectory() as td:
            path = write(Path(td), "nodesc", "---\nname: nodesc\n"
                                    "license: MIT\n---\nBody.\n")
            skill = load_skill_file(path)
        self.assertTrue(skill.description_parse_failed)
        self.assertEqual(skill.description, "")

    def test_url_value_with_colons_is_not_split_into_keys(self):
        # A bare URL contains ":" but is a value, never a nested key. A line
        # scanner splits it at the first colon, losing the scheme and host.
        text = "---\nname: u\ngithub: https://example.com/a/b\ndescription: Route here.\n---\nBody.\n"
        self.assertEqual(_parse_frontmatter(text)["github"], "https://example.com/a/b")
        with tempfile.TemporaryDirectory() as td:
            skill = load_skill_file(write(Path(td), "u", text))
        self.assertEqual(skill.description, "Route here.")

    def test_trailing_comment_is_stripped_but_attached_hash_is_kept(self):
        # YAML: " #" starts a comment, a "#" glued to a word does not.
        # Verified against PyYAML — "keep #hashtag intact" really does parse
        # as "keep", so an audit that disagrees with the agent's own loader
        # would judge the routing text the agent never saw.
        parsed = _parse_frontmatter(
            "---\nname: c\n"
            "description: real routing text  # a note\n"
            "other: keep#hashtag intact\n"
            "link: https://example.com/page#section\n---\nB\n"
        )
        self.assertEqual(parsed["description"], "real routing text")
        self.assertEqual(parsed["other"], "keep#hashtag intact")
        self.assertEqual(parsed["link"], "https://example.com/page#section")

    def test_quoted_value_keeps_its_own_colon(self):
        parsed = _parse_frontmatter(
            '---\nname: q\ndescription: "Use when: the user asks"\n---\nB\n'
        )
        self.assertEqual(parsed["description"], "Use when: the user asks")


class LoadInjectedTest(unittest.TestCase):
    def _write_json(self, tmp, payload):
        p = tmp / "truth.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_flat_shape(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write_json(tmp, {
                "library": {"description": "Route here for pages and docs.",
                            "intent": "pages, docs, tables"},
            })
            skills = load_injected(p)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "library")
        self.assertTrue(skills[0].is_injected)
        self.assertEqual(skills[0].intent, "pages, docs, tables")

    def test_wrapper_shape_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write_json(tmp, {"skills": {
                "cloud-service": {"description": "Backend for sites."}}})
            skills = load_injected(p)
        self.assertEqual([s.name for s in skills], ["cloud-service"])

    def test_entry_with_neither_description_nor_body_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write_json(tmp, {"empty": {"intent": "nothing to route on"}})
            self.assertEqual(load_injected(p), [])

    def test_unreadable_body_keeps_the_description(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._write_json(tmp, {
                "ghost": {"description": "Still routable.",
                          "body_path": str(tmp / "nope" / "SKILL.md")}})
            skills = load_injected(p)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].description, "Still routable.")
        self.assertFalse(skills[0].description_parse_failed)

    def test_malformed_json_degrades_to_empty(self):
        # One unreadable file must not take the audit down.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_injected(p), [])

    def test_body_path_body_is_stripped_of_its_own_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = write(tmp, "real", "---\nname: real\n"
                                     "description: A real one.\n---\nActual body text.\n")
            p = self._write_json(tmp, {
                "real": {"description": "Supplied.", "body_path": str(body)}})
            skills = load_injected(p)
        self.assertNotIn("description:", skills[0].trigger_excerpt)
        self.assertIn("Actual body text.", skills[0].trigger_excerpt)


class MergeCatalogTest(unittest.TestCase):
    def _catalog_skill(self, name, description):
        return Skill(name=name, description=description,
                     trigger_excerpt=description, path=Path("<catalog>"))

    def test_injected_replaces_same_named_catalog_entry(self):
        merged = merge_catalog(
            [self._catalog_skill("make-bot-ui", ">-")],
            [self._catalog_skill("make-bot-ui", "Real routing text.")],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].description, "Real routing text.")

    def test_unknown_name_is_added_not_dropped(self):
        merged = merge_catalog(
            [self._catalog_skill("a", "A.")],
            [self._catalog_skill("cloud-service", "Backend.")],
        )
        self.assertEqual(sorted(s.name for s in merged), ["a", "cloud-service"])

    def test_merge_is_case_insensitive_on_name(self):
        merged = merge_catalog(
            [self._catalog_skill("Tencent-PPTX", "old")],
            [self._catalog_skill("tencent-pptx", "new")],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].description, "new")

    def test_no_injection_leaves_catalog_untouched(self):
        catalog = [self._catalog_skill("a", "A.")]
        self.assertEqual(merge_catalog(catalog, None), catalog)
        self.assertEqual(merge_catalog(catalog, []), catalog)


if __name__ == "__main__":
    unittest.main()