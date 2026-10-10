# tests/test_ir_schema.py
import json
import unittest

from agent_session_detective import __version__
from agent_session_detective.ir.schema import (
    BUCKETS,
    BilledUsage,
    IR_VERSION,
    UNATTRIBUTED,
    AuditDocument,
    ContentItem,
    ContextAgent,
    CoverageReport,
    ItemRef,
    LLMCall,
    Observation,
    RequestInput,
    RequestOutput,
    SkillEntity,
    Span,
)


def _item(**overrides):
    fields = dict(
        item_id="main:1:0",
        agent_id="main",
        bucket="user",
        kind="user_message",
        name=None,
        channel="qoder:user",
        wire_seq=1,
        gone_seq=None,
        size_chars=10,
        tokens_est=2,
        sha1="a" * 40,
        norm_sha1="b" * 40,
        record={"file": "t.jsonl", "seq": 1, "uuid": "u1"},
        preview="audit this",
        skill_id=None,
    )
    fields.update(overrides)
    return ContentItem(**fields)


def _minimal_document():
    call = LLMCall(
        call_id="main:0",
        agent_id="main",
        ts=1.0,
        model="qoder-model",
        span=Span(first_seq=1, last_seq=2, n_records=2),
        identity_tier=1,
        input=RequestInput(
            item_refs=[ItemRef(item_id="main:1:0", bucket="user", tokens_est=2)],
            buckets={bucket: 0 for bucket in BUCKETS},
            anchor_tokens=10,
            unattributed_tokens=8,
            cache_read=0,
            cache_write_5m=0,
            cache_write_1h=0,
            context_usage_ratio=None,
            request_id="req-1",
            request_hash="a" * 64,
            response_hash="b" * 64,
            credits=None,
            prefix_hashes=None,
        ),
        output=RequestOutput(parts=[], output_tokens=0),
    )
    call.input.buckets["user"] = 2
    agent = ContextAgent(
        agent_id="main",
        parent_id=None,
        origin_channel="qoder:main",
        model="qoder-model",
        context_window=128000,
        request_ids=["main:0"],
        active_leaf=None,
    )
    coverage = CoverageReport(
        per_field={},
        requests_with_anchor="1/1",
        request_identity={},
        bucket_sources={},
        unknown_channels=[],
        dropped_records={},
        notes=[],
    )
    return AuditDocument(
        adapter={"id": "qoder", "version": "1.0"},
        estimator_version="cjk-1.0",
        source_files=["t.jsonl"],
        agents=[agent],
        requests=[call],
        items=[_item()],
        skills=[
            SkillEntity(
                skill_id="demo",
                name="demo",
                observations=[
                    Observation(
                        kind="listing",
                        channel="skill_listing",
                        agent_id="main",
                        call_id="main:0",
                        ts=1.0,
                        tokens_est=2,
                        body_sha1=None,
                        item_id="main:1:0",
                    )
                ],
            )
        ],
        compactions=[],
        coverage=coverage,
    )


class SchemaTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(IR_VERSION, "1.5")
        self.assertEqual(
            BUCKETS,
            ("system", "tools", "user", "inject", "skill", "assistant", "tool"),
        )
        self.assertEqual(UNATTRIBUTED, "unattributed")

    def test_document_round_trips_through_json(self):
        document = _minimal_document()
        payload = document.to_dict()
        revived = json.loads(json.dumps(payload))
        self.assertEqual(revived["ir_version"], "1.5")
        self.assertEqual(revived["adapter"], {"id": "qoder", "version": "1.0"})
        self.assertEqual(revived["requests"][0]["call_id"], "main:0")
        self.assertEqual(revived["items"][0]["bucket"], "user")
        self.assertEqual(revived["skills"][0]["observations"][0]["kind"], "listing")

    def test_human_text_defaults_to_none_and_round_trips(self):
        payload = _minimal_document().to_dict()
        self.assertIsNone(payload["items"][0]["human_text"])

        typed = _minimal_document()
        typed.items[0].human_text = "/goal 制定计划"
        revived = json.loads(json.dumps(typed.to_dict()))
        self.assertEqual(revived["items"][0]["human_text"], "/goal 制定计划")

    def test_generator_reports_package_version(self):
        self.assertEqual(
            _minimal_document().generator,
            {"name": "agent-session-detective", "version": __version__},
        )

    def test_document_top_level_keys_are_exact(self):
        payload = _minimal_document().to_dict()
        self.assertEqual(
            sorted(payload),
            [
                "adapter",
                "agents",
                "billing",
                "compactions",
                "coverage",
                "dispatches",
                "estimator_version",
                "generator",
                "ir_version",
                "items",
                "phases",
                "requests",
                "skill_loads",
                "skills",
                "source_files",
            ],
        )
        self.assertEqual(sorted(payload["generator"]), ["name", "version"])

    def test_nested_call_and_item_keys(self):
        payload = _minimal_document().to_dict()
        call = payload["requests"][0]
        self.assertEqual(
            sorted(call),
            ["agent_id", "call_id", "identity_tier", "input", "model", "output", "span", "ts"],
        )
        self.assertEqual(
            sorted(call["input"]),
            [
                "anchor_tokens",
                "buckets",
                "cache_read",
                "cache_write_1h",
                "cache_write_5m",
                "context_usage_ratio",
                "credits",
                "item_refs",
                "prefix_hashes",
                "request_hash",
                "request_id",
                "response_hash",
                "unattributed_tokens",
            ],
        )
        item = payload["items"][0]
        self.assertEqual(
            sorted(item),
            [
                "agent_id",
                "bucket",
                "channel",
                "gone_seq",
                "human_text",
                "item_id",
                "kind",
                "name",
                "norm_sha1",
                "phase_id",
                "preview",
                "record",
                "sha1",
                "size_chars",
                "skill_id",
                "tokens_est",
                "tool_use_id",
                "wire_seq",
            ],
        )


class BilledUsageTest(unittest.TestCase):
    def _document(self):
        return AuditDocument(
            adapter={"id": "test", "version": "0"},
            estimator_version="test",
            source_files=[],
            agents=[],
            requests=[],
            items=[],
            skills=[],
            compactions=[],
            coverage=CoverageReport(
                per_field={}, requests_with_anchor="", request_identity={},
                bucket_sources={}, unknown_channels=[], dropped_records={},
                notes=[]),
        )

    def _billed(self, **overrides):
        fields = dict(
            session_id="3b241101-e2bb-4255-8caf-4136c566a962",
            source="SharedClientCache chat_message.token_info",
            db_path="/tmp/local.db",
            requests=2,
            prompt_tokens=100,
            completion_tokens=10,
            cached_tokens=80,
            rows_total=3,
            rows_without_token_info=1,
        )
        fields.update(overrides)
        return BilledUsage(**fields)

    def test_billing_field_defaults_to_none(self):
        self.assertIsNone(self._document().billing)

    def test_billing_serializes_before_ir_version(self):
        document = self._document()
        document.billing = self._billed()
        payload = document.to_dict()
        keys = list(payload.keys())
        self.assertLess(keys.index("billing"), keys.index("ir_version"))
        self.assertEqual(payload["billing"]["prompt_tokens"], 100)
        self.assertEqual(payload["billing"]["rows_without_token_info"], 1)
