# tests/ir_helpers.py
"""Shared IR test helpers: Session fixtures and adapter-contract assertions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_detective.wire import load_session


class IREventsTestCase(unittest.TestCase):
    def load_lines(self, lines, subagents=None, subagent_metas=None,
                   name="transcript.jsonl"):
        """Write ``lines`` (list of dicts) to a temp dir and load the session.

        ``subagents`` maps stem -> list of record dicts, written to
        ``<parent-stem>/subagents/<stem>.jsonl`` next to the transcript.
        ``subagent_metas`` maps stem -> meta dict, written as the
        ``<stem>.meta.json`` sidecar the wire loader reads.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / name
        source.write_text(
            "\n".join(json.dumps(line) for line in lines), encoding="utf-8"
        )
        for stem, records in (subagents or {}).items():
            sub_dir = root / source.stem / "subagents"
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / ("%s.jsonl" % stem)).write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            meta = (subagent_metas or {}).get(stem)
            if meta is not None:
                (sub_dir / ("%s.meta.json" % stem)).write_text(
                    json.dumps(meta), encoding="utf-8"
                )
        return load_session(source)


def assert_adapter_contract(test, document):
    """Adapter-independent invariants of an ``AuditDocument``.

    Imports are local so the lower-layer suites that import this module
    (records, items, spans) stay independent of the builder.
    """
    from agent_session_detective.ir.builder import (
        OUTPUT_PART_TYPES,
        alive_items_for_call,
    )
    from agent_session_detective.ir.schema import BUCKETS

    items_by_id = {item.item_id: item for item in document.items}
    test.assertEqual(len(items_by_id), len(document.items))
    calls_by_id = {call.call_id: call for call in document.requests}
    test.assertEqual(len(calls_by_id), len(document.requests))
    agent_ids = {agent.agent_id for agent in document.agents}
    skill_ids = {skill.skill_id for skill in document.skills}
    raw_skill_ids = {
        observation.raw_skill_id
        for skill in document.skills
        for observation in skill.observations
        if observation.raw_skill_id is not None
    }

    for call in document.requests:
        test.assertIn(call.agent_id, agent_ids)
        test.assertIn(call.identity_tier, (1, 2))
        test.assertEqual(sorted(call.input.buckets), sorted(BUCKETS))
        total = sum(call.input.buckets.values())
        if call.input.anchor_tokens is None:
            test.assertIsNone(call.input.unattributed_tokens)
        else:
            test.assertEqual(
                call.input.unattributed_tokens,
                call.input.anchor_tokens - total,
            )
        alive = alive_items_for_call(document.items, call)
        test.assertEqual(
            [item.item_id for item in alive],
            [ref.item_id for ref in call.input.item_refs],
        )
        for ref in call.input.item_refs:
            item = items_by_id[ref.item_id]
            test.assertEqual(ref.bucket, item.bucket)
            test.assertEqual(ref.tokens_est, item.tokens_est)
        if call.identity_tier == 1:
            test.assertIsNotNone(call.input.request_id)
            test.assertIsNotNone(call.input.request_hash)
        for part in call.output.parts:
            item = items_by_id[part.item_id]
            test.assertEqual(item.agent_id, call.agent_id)
            test.assertEqual(part.type, OUTPUT_PART_TYPES.get(item.kind))
            test.assertLessEqual(call.span.first_seq, item.wire_seq)
            test.assertLessEqual(item.wire_seq, call.span.last_seq)

    for agent in document.agents:
        own_calls = [
            call for call in document.requests if call.agent_id == agent.agent_id
        ]
        test.assertEqual(agent.request_ids, [call.call_id for call in own_calls])
        ordered = sorted(own_calls, key=lambda call: call.span.first_seq)
        for previous, current in zip(ordered, ordered[1:]):
            test.assertLess(previous.span.last_seq, current.span.first_seq)

    for item in document.items:
        if item.skill_id is not None:
            test.assertIn(item.skill_id, skill_ids | raw_skill_ids)

    for skill in document.skills:
        for observation in skill.observations:
            test.assertIn(observation.item_id, items_by_id)
            test.assertIn(observation.agent_id, agent_ids)
            if observation.call_id is not None:
                test.assertIn(observation.call_id, calls_by_id)

    for compaction in document.compactions:
        test.assertIn(compaction.agent_id, agent_ids)
        for item_id in compaction.restored_item_ids:
            test.assertIn(item_id, items_by_id)

    test.assertEqual(
        document.coverage.bucket_sources["envelope"]
        + document.coverage.bucket_sources["signature"],
        len(document.items),
    )
    test.assertLessEqual(
        document.coverage.request_identity["derived_verification"][
            "checked_calls"
        ],
        sum(1 for call in document.requests if call.identity_tier == 1),
    )
