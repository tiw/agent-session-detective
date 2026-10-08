import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_detective.ir.items import extract_items
from agent_session_detective.ir.records import group_records
from agent_session_detective.wire import load_session
from tests.ir_helpers import IREventsTestCase


def user(seq, text, uuid=None):
    return {
        "type": "user",
        "uuid": uuid or "u%d" % seq,
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def assistant(seq, content, usage=None):
    record = {
        "type": "assistant",
        "uuid": "a%d" % seq,
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "message": {"content": content},
    }
    if usage is not None:
        record["message"]["usage"] = usage
    return record


def tool_result(seq, tool_call_id, content, is_error=False):
    return {
        "type": "user",
        "uuid": "r%d" % seq,
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_call_id,
                                 "content": content, "is_error": is_error}]},
    }


def attachment(seq, attachment_type, payload):
    return {
        "type": "attachment",
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "attachment": dict(payload, type=attachment_type),
    }


USAGE = {"input_tokens": 120, "output_tokens": 30}


class ItemsTest(IREventsTestCase):
    def extract(self, lines, agent_id="main"):
        records = group_records(self.load_lines(lines).events)
        return extract_items(records, agent_id)

    def test_assistant_parts_and_tool_calls_itemized_in_event_order(self):
        extraction = self.extract(
            [
                user(1, "audit this"),
                assistant(2, [
                    {"type": "text", "text": "I will inspect it."},
                    {"type": "thinking", "thinking": "inspect trace"},
                    {"type": "tool_use", "id": "tool-skill", "name": "Skill",
                     "input": {"skill": "superpowers:brainstorming"}},
                    {"type": "tool_use", "id": "tool-read", "name": "Read",
                     "input": {"file_path": "/tmp/demo/SKILL.md"}},
                ], usage=USAGE),
            ]
        )
        items = extraction.items

        self.assertEqual(
            [item.item_id for item in items],
            ["main:1:0", "main:2:0", "main:2:1", "main:2:2", "main:2:3"],
        )
        self.assertEqual(
            [item.bucket for item in items],
            ["user", "assistant", "assistant", "assistant", "assistant"],
        )
        self.assertEqual(
            [item.kind for item in items],
            ["user_message", "assistant_text", "thinking", "tool_call", "tool_call"],
        )
        self.assertEqual(items[2].tokens_est, 3)  # "inspect trace" -> 13 // 4
        self.assertEqual(items[3].skill_id, "superpowers:brainstorming")
        self.assertIsNone(items[4].skill_id)

    def test_read_of_skill_file_gives_result_item_the_skill_id(self):
        extraction = self.extract(
            [
                assistant(1, [
                    {"type": "tool_use", "id": "tool-read", "name": "Read",
                     "input": {"file_path": "/tmp/demo/SKILL.md"}},
                ], usage=USAGE),
                tool_result(2, "tool-read", "plain skill file"),
            ]
        )
        call_item, result_item = extraction.items

        self.assertEqual(call_item.kind, "tool_call")
        self.assertIsNone(call_item.skill_id)
        self.assertEqual(result_item.item_id, "main:2:0")
        self.assertEqual(result_item.bucket, "tool")
        self.assertEqual(result_item.kind, "tool_result")
        self.assertEqual(result_item.skill_id, "demo")

    def test_launching_skill_result_becomes_stub(self):
        extraction = self.extract(
            [tool_result(1, "tool-skill", "Launching skill: demo-psychology")]
        )
        item = extraction.items[0]

        self.assertEqual(item.bucket, "skill")
        self.assertEqual(item.kind, "skill_stub")
        self.assertEqual(item.skill_id, "demo-psychology")

    def test_user_message_with_skill_content_is_signature_body(self):
        extraction = self.extract(
            [user(1, '<skill_content name="demo">Body</skill_content>')]
        )
        item = extraction.items[0]

        self.assertEqual(item.bucket, "skill")
        self.assertEqual(item.kind, "skill_body")
        self.assertEqual(item.channel, "qoder:signature:skill_body")
        self.assertEqual(item.skill_id, "demo")
        self.assertEqual(extraction.signature, 1)
        self.assertEqual(extraction.envelope, 0)

    def test_compact_summary_turn_goes_to_inject(self):
        extraction = self.extract(
            [
                {
                    "type": "user",
                    "uuid": "u1",
                    "isCompactSummary": True,
                    "timestamp": "2026-10-07T08:00:01.000Z",
                    "message": {"content": [{"type": "text", "text": "summary of prior work"}]},
                }
            ]
        )
        item = extraction.items[0]

        self.assertEqual(item.bucket, "inject")
        self.assertEqual(item.kind, "compact_summary")
        self.assertEqual(item.channel, "qoder:user:compact_summary")

    def test_skill_listing_attachment_yields_catalog_and_listing_lines(self):
        extraction = self.extract(
            [
                attachment(1, "skill_listing",
                           {"content": "- demo: A demo skill\n- other: Another skill"}),
            ]
        )
        item = extraction.items[0]

        self.assertEqual(item.bucket, "skill")
        self.assertEqual(item.kind, "skill_catalog")
        self.assertEqual(item.channel, "qoder:attachment:skill_listing")
        self.assertEqual(item.tokens_est, 10)
        self.assertEqual(
            extraction.listing_lines[item.item_id],
            [("demo", "demo", 5), ("other", "other", 5)],
        )
        self.assertEqual(extraction.envelope, 1)
        self.assertEqual(extraction.signature, 0)

    def test_invoked_skills_attachment_yields_one_item_per_entry(self):
        extraction = self.extract(
            [
                attachment(1, "invoked_skills", {"skills": [
                    {"name": "demo", "content": "Body one"},
                    {"name": "other", "content": "Body two"},
                ]}),
            ]
        )
        items = extraction.items

        self.assertEqual([item.item_id for item in items], ["main:1:0", "main:1:1"])
        self.assertTrue(all(item.bucket == "skill" for item in items))
        self.assertEqual([item.skill_id for item in items], ["demo", "other"])
        self.assertEqual([item.name for item in items], ["demo", "other"])
        self.assertEqual(extraction.envelope, 2)

    def test_hook_output_signature_branch_and_inline_branch(self):
        extraction = self.extract(
            [
                attachment(1, "hook_output",
                           {"hookEventName": "SessionStart", "output": "plain hook text"}),
                attachment(2, "hook_output",
                           {"hookEventName": "SessionStart",
                            "output": '<skill_content name="demo">Body</skill_content>'}),
            ]
        )
        inline, body = extraction.items

        self.assertEqual((inline.bucket, inline.kind), ("inject", "hook_inline"))
        self.assertEqual(inline.channel, "qoder:attachment:hook_output")
        self.assertEqual((body.bucket, body.kind), ("skill", "skill_body"))
        self.assertEqual(body.skill_id, "demo")
        self.assertEqual(extraction.envelope, 1)
        self.assertEqual(extraction.signature, 1)
        self.assertEqual(extraction.conflicts, 0)

    def test_unknown_attachment_goes_to_unattributed(self):
        extraction = self.extract([attachment(1, "brand_new_kind", {"whatever": "x"})])
        item = extraction.items[0]

        self.assertEqual(item.bucket, "unattributed")
        self.assertEqual(item.kind, "unknown")
        self.assertEqual(item.channel, "qoder:attachment:brand_new_kind")
        self.assertEqual(extraction.envelope, 1)

    def test_signature_in_non_skill_envelope_is_a_conflict(self):
        extraction = self.extract(
            [
                attachment(1, "critical_system_reminder",
                           {"content": 'reminder <skill_content name="demo">Body</skill_content>'}),
            ]
        )
        item = extraction.items[0]

        self.assertEqual(item.bucket, "inject")
        self.assertEqual(item.kind, "reminder")
        self.assertIsNone(item.skill_id)
        self.assertEqual(extraction.conflicts, 1)
        self.assertEqual(extraction.signature, 0)
        self.assertEqual(extraction.envelope, 1)

    def test_tool_result_structured_output_is_flattened(self):
        extraction = self.extract(
            [tool_result(1, "tool-b", [{"type": "text", "text": "hello world"}])]
        )
        self.assertEqual(extraction.items[0].size_chars, len("hello world"))

    def test_item_identity_fields_sha1_norm_sha1_preview_and_record(self):
        text = "2026-10-07T08:00:00.000Z\naudit this"
        extraction = self.extract([user(1, text, uuid="u1")])
        item = extraction.items[0]

        self.assertEqual(item.size_chars, len(text))
        self.assertEqual(item.preview, text)
        self.assertEqual(item.sha1, hashlib.sha1(text.encode("utf-8")).hexdigest())
        self.assertEqual(
            item.norm_sha1, hashlib.sha1(b"audit this").hexdigest()
        )
        self.assertEqual(item.record["seq"], 1)
        self.assertEqual(item.record["uuid"], "u1")
        self.assertTrue(item.record["file"].endswith("transcript.jsonl"))

    def test_failed_skill_file_read_keeps_no_skill_id(self):
        extraction = self.extract(
            [
                assistant(1, [
                    {"type": "tool_use", "id": "tool-read", "name": "Read",
                     "input": {"file_path": "/tmp/demo/SKILL.md"}},
                ], usage=USAGE),
                tool_result(2, "tool-read", "ENOENT: no such file", is_error=True),
            ]
        )
        result_item = extraction.items[1]

        self.assertEqual(result_item.bucket, "tool")
        self.assertEqual(result_item.kind, "tool_result")
        self.assertIsNone(result_item.skill_id)
        self.assertIsNone(result_item.name)

    def test_null_text_part_degrades_to_empty(self):
        extraction = self.extract([user(1, None)])
        item = extraction.items[0]

        self.assertEqual(item.bucket, "user")
        self.assertEqual(item.size_chars, 0)
        self.assertEqual(item.tokens_est, 0)

    def test_tool_result_image_block_contributes_no_text(self):
        extraction = self.extract(
            [tool_result(1, "tool-b", [
                {"type": "image", "media_type": "image/png", "data": "aGVsbG8="},
                {"type": "text", "text": "described"},
            ])]
        )
        item = extraction.items[0]

        self.assertEqual(item.size_chars, len("described"))
        self.assertNotIn("aGVsbG8=", item.preview)

    def test_non_string_assistant_text_degrades_to_empty(self):
        extraction = self.extract(
            [assistant(1, [{"type": "text", "text": 123}], usage=USAGE)]
        )
        item = extraction.items[0]

        self.assertEqual(item.bucket, "assistant")
        self.assertEqual(item.size_chars, 0)
        self.assertEqual(item.tokens_est, 0)

    def test_null_user_input_degrades_to_empty(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "wire.jsonl").write_text(
            json.dumps({
                "timestamp": "2026-10-07T08:00:01.000Z",
                "message": {"type": "TurnBegin",
                            "payload": {"user_input": None}},
            }),
            encoding="utf-8",
        )
        records = group_records(load_session(root).events)
        extraction = extract_items(records, "main")
        item = extraction.items[0]

        self.assertEqual(item.bucket, "user")
        self.assertEqual(item.kind, "user_message")
        self.assertEqual(item.size_chars, 0)


if __name__ == "__main__":
    unittest.main()
