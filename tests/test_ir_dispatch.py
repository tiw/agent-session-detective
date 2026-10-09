"""Spec items 3-5 — dispatch join, brief classification (D3), phase tiers.

Join fixtures are built through the wire loader (``load_lines``) so the
``parent_tool_use_id`` / meta-sidecar facts flow through the real parser;
phase tiers run ``build_dispatches`` directly against a fixture rule set so
the shipped-empty ``RULES`` never influence these verdicts.
"""

import unittest

from agent_session_detective.ir.builder import build_audit_document
from agent_session_detective.ir.dispatch import build_dispatches
from agent_session_detective.ir.items import extract_items
from agent_session_detective.ir.records import attribute_agents, group_records
from agent_session_detective.ir.schema import IR_VERSION
from tests.ir_helpers import IREventsTestCase, assert_adapter_contract

# A fixture rule set (the shipped RULES stay empty): two phases, matched on
# brief text and on Read file paths.
FIXTURE_RULES = [
    {
        "phase_id": "phase-alpha",
        "name": "Alpha phase",
        "patterns": [
            {"regex": r"alpha[- ]flow", "target": "brief", "ref_kind": "unknown"},
            {"regex": r"skills/alpha/SKILL\.md$", "target": "read",
             "ref_kind": "skill_file"},
        ],
    },
    {
        "phase_id": "phase-beta",
        "name": "Beta phase",
        "patterns": [
            {"regex": r"beta[- ]flow", "target": "brief", "ref_kind": "unknown"},
        ],
    },
]


def _clock():
    state = {"n": 0}

    def ts():
        state["n"] += 1
        return "2026-10-08T12:00:%02d.000Z" % (state["n"] % 60)

    return ts


def user_record(ts, uuid, text, parent=None, parent_tool=None):
    rec = {"type": "user", "uuid": uuid, "parentUuid": parent,
           "isSidechain": False, "timestamp": ts,
           "message": {"content": [{"type": "text", "text": text}]}}
    if parent_tool is not None:
        rec["parentToolUseId"] = parent_tool
    return rec


def assistant_record(ts, uuid, text=None, tool_use=None, request_id=None,
                     input_tokens=2000, parent=None):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_use is not None:
        tool_id, name, tool_input = tool_use
        content.append(
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
        )
    message = {"model": "qoder-pro", "content": content}
    if request_id is not None:
        message["usage"] = {"input_tokens": input_tokens, "output_tokens": 10,
                            "request_id": request_id}
    rec = {"type": "assistant", "uuid": uuid, "parentUuid": parent,
           "isSidechain": False, "timestamp": ts, "message": message}
    if request_id is not None:
        rec["requestTokenAnchor"] = {
            "request": "hash-r-" + request_id,
            "response": "hash-s-" + request_id,
            "requestId": request_id,
        }
    return rec


def join_session(test):
    """Three parent Agent calls: one joined via records, one via meta
    sidecar only, one orphan; plus a subagent file matching nothing."""
    ts = _clock()
    main_lines = [
        user_record(ts(), "m1", "kick off"),
        assistant_record(
            ts(), "m2",
            tool_use=("call_00A", "Agent",
                      {"subagent_type": "code", "description": "spawned worker"}),
            request_id="req-1", parent="m1"),
        assistant_record(
            ts(), "m3", tool_use=("call_00B", "Agent", {}),
            request_id="req-2", parent="m2"),
        assistant_record(
            ts(), "m4",
            tool_use=("call_00C", "Agent",
                      {"subagent_type": "code", "description": "never joined"}),
            request_id="req-3", parent="m3"),
    ]
    subagents = {
        "worker": [
            user_record(ts(), "w1", "alpha-flow investigate the bug",
                        parent_tool="call_00A"),
            assistant_record(ts(), "w2", text="on it", request_id="req-w1",
                             parent="w1"),
        ],
        "meta-joined": [
            # No parentToolUseId anywhere: only the sidecar can join it.
            user_record(ts(), "j1", "please inspect the module"),
            assistant_record(ts(), "j2", text="ok", request_id="req-j1",
                             parent="j1"),
        ],
        "loner": [
            # References a parent id that does not exist: unmatched file.
            user_record(ts(), "l1", "orphan transcript",
                        parent_tool="call_missing"),
            assistant_record(ts(), "l2", text="done", request_id="req-l1",
                             parent="l1"),
        ],
    }
    metas = {"meta-joined": {"toolUseId": "call_00B", "agentType": "code",
                             "description": "joined by sidecar only"}}
    return test.load_lines(main_lines, subagents=subagents, subagent_metas=metas)


def audit(test):
    return build_audit_document(join_session(test), "qoder")


def project_dispatches(session, rules, rule_set_version):
    """Run build_dispatches directly with an explicit rule set.

    Returns ``((dispatches, phases, links, recognition), items)`` — the
    items are the mutated shared objects, so phase stamps can be asserted.
    """
    records = group_records(session.events)
    agents = attribute_agents(records)
    items = []
    seq_ts = {}
    for agent in agents:
        items.extend(extract_items(agent.records, agent.agent_id).items)
        for record in agent.records:
            seq_ts[(record.source, record.seq)] = record.ts
    result = build_dispatches(agents, items, session.subagent_meta, seq_ts,
                              rules=rules, rule_set_version=rule_set_version)
    return result, items


def single_dispatch_session(test, brief_text, sub_records=()):
    """Main makes one Agent call; one subagent with the given brief."""
    ts = _clock()
    main_lines = [
        user_record(ts(), "m1", "go"),
        assistant_record(ts(), "m2",
                         tool_use=("call_x", "Agent", {"subagent_type": "code"}),
                         request_id="req-1", parent="m1"),
    ]
    records = [user_record(ts(), "s1", brief_text, parent_tool="call_x")]
    records.extend(sub_records)
    return test.load_lines(main_lines, subagents={"probe": records})


class DispatchJoinTest(IREventsTestCase, unittest.TestCase):
    def test_join_fixture_covers_both_directions_and_the_meta_fallback(self):
        document = audit(self)
        dispatches = document.dispatches

        self.assertEqual([d.dispatch_id for d in dispatches],
                         ["main:dispatch:1", "main:dispatch:2",
                          "main:dispatch:3"])
        first, second, orphan = dispatches
        self.assertEqual(first.tool_use_id, "call_00A")
        self.assertEqual(first.subagent_agent_id, "subagent:worker")
        self.assertIsNotNone(first.brief_item_id)
        self.assertGreater(first.brief_tokens_est, 0)
        self.assertIsNone(first.meta)
        tool_item = {i.item_id: i for i in document.items}[first.tool_item_id]
        self.assertEqual((tool_item.kind, tool_item.tool_use_id),
                         ("tool_call", "call_00A"))

        # meta.json toolUseId fallback, counted joined_via_meta_only; the
        # sidecar also fills subagent_type/description the call omitted.
        self.assertEqual(second.tool_use_id, "call_00B")
        self.assertEqual(second.subagent_agent_id, "subagent:meta-joined")
        self.assertEqual(
            second.meta,
            {"toolUseId": "call_00B", "agentType": "code",
             "description": "joined by sidecar only"},
        )
        self.assertEqual(second.subagent_type, "code")
        self.assertEqual(second.description, "joined by sidecar only")

        # Orphan parent call: a row exists, the subagent id stays null.
        self.assertEqual(orphan.tool_use_id, "call_00C")
        self.assertIsNone(orphan.subagent_agent_id)
        self.assertIsNone(orphan.brief_item_id)

        links = document.coverage.dispatch_links
        self.assertEqual(links, {
            "dispatches": 3,
            "joined": 1,
            "joined_via_meta_only": 1,
            "orphan_dispatches": 1,
            "unmatched_subagent_files": 1,
            "meta_files_loaded": 1,
            "briefs_found": 2,
            "briefs_missing": 1,
        })
        recognition = document.coverage.phase_recognition
        self.assertEqual(
            (recognition["tierA"], recognition["tierB"], recognition["tierC"]),
            (0, 0, 3),
        )
        self.assertIsNone(recognition["rule_set_version"])
        assert_adapter_contract(self, document)

    def test_d3_rebucketing_is_reflected_in_the_bucket_tallies(self):
        document = audit(self)
        items_by_id = {i.item_id: i for i in document.items}
        brief = items_by_id[document.dispatches[0].brief_item_id]
        self.assertEqual((brief.kind, brief.bucket), ("dispatch_brief", "inject"))

        worker_call = next(c for c in document.requests
                           if c.agent_id == "subagent:worker")
        buckets = worker_call.input.buckets
        self.assertEqual(buckets["inject"], brief.tokens_est)
        self.assertEqual(buckets["user"], 0)
        self.assertEqual(
            worker_call.input.unattributed_tokens,
            worker_call.input.anchor_tokens - sum(buckets.values()),
        )
        self.assertEqual(worker_call.input.anchor_tokens, 2000)

    def test_briefs_are_reclassified_even_where_the_join_fails(self):
        document = audit(self)
        items_by_id = {i.item_id: i for i in document.items}
        loner_briefs = [
            i for i in document.items
            if i.agent_id == "subagent:loner" and i.kind == "dispatch_brief"
        ]
        self.assertEqual(len(loner_briefs), 1)
        self.assertEqual(loner_briefs[0].bucket, "inject")
        self.assertNotIn(loner_briefs[0].item_id,
                         [d.brief_item_id for d in document.dispatches])
        # the shared items list is the single source: no stale user_message
        # twin of the same record survives next to the reclassified item
        user_twins = [
            i for i in document.items
            if i.agent_id == "subagent:loner" and i.kind == "user_message"
        ]
        self.assertEqual(user_twins, [])

    def test_join_is_string_equality_on_ids_never_format_parsing(self):
        ts = _clock()
        main_lines = [
            user_record(ts(), "m1", "go"),
            assistant_record(ts(), "m2",
                             tool_use=("call_00ABC", "Agent",
                                       {"subagent_type": "code"}),
                             request_id="req-1", parent="m1"),
            assistant_record(ts(), "m3",
                             tool_use=("call_00ABCextra", "Agent",
                                       {"subagent_type": "code"}),
                             request_id="req-2", parent="m2"),
        ]
        subagents = {
            "decoy": [
                user_record(ts(), "d1", "the real target", parent_tool="call_00ABCextra"),
                assistant_record(ts(), "d2", text="hi", request_id="req-d1",
                                 parent="d1"),
            ],
        }
        document = build_audit_document(
            self.load_lines(main_lines, subagents=subagents), "qoder"
        )

        self.assertEqual(len(document.dispatches), 2)
        orphan, joined = document.dispatches
        # The subagent joins ONLY the exact id it names — an implementation
        # that parses id formats would pair it with "call_00ABC" instead.
        self.assertIsNone(orphan.subagent_agent_id)
        self.assertEqual(orphan.tool_use_id, "call_00ABC")
        self.assertEqual(joined.tool_use_id, "call_00ABCextra")
        joined_item = {i.item_id: i for i in document.items}[joined.tool_item_id]
        self.assertEqual(joined_item.tool_use_id, "call_00ABCextra")

    def test_nested_subagent_to_subagent_dispatches_join(self):
        ts = _clock()
        main_lines = [
            user_record(ts(), "m1", "go"),
            assistant_record(ts(), "m2",
                             tool_use=("call_outer", "Agent",
                                       {"subagent_type": "code"}),
                             request_id="req-1", parent="m1"),
        ]
        subagents = {
            "outer": [
                user_record(ts(), "o1", "outer brief", parent_tool="call_outer"),
                assistant_record(ts(), "o2",
                                 tool_use=("call_inner", "Agent",
                                           {"subagent_type": "code"}),
                                 request_id="req-o1", parent="o1"),
            ],
            "inner": [
                user_record(ts(), "i1", "inner brief", parent_tool="call_inner"),
                assistant_record(ts(), "i2", text="done", request_id="req-i1",
                                 parent="i1"),
            ],
        }
        document = build_audit_document(
            self.load_lines(main_lines, subagents=subagents), "qoder"
        )

        self.assertEqual(
            [(d.dispatch_id, d.parent_agent_id, d.subagent_agent_id)
             for d in document.dispatches],
            [
                ("main:dispatch:1", "main", "subagent:outer"),
                ("subagent:outer:dispatch:1", "subagent:outer", "subagent:inner"),
            ],
        )
        links = document.coverage.dispatch_links
        self.assertEqual(
            (links["dispatches"], links["joined"], links["orphan_dispatches"],
             links["unmatched_subagent_files"]),
            (2, 2, 0, 0),
        )


class BriefClassificationTest(IREventsTestCase, unittest.TestCase):
    def test_first_user_record_becomes_the_brief_and_is_reclassified(self):
        ts = _clock()
        main_lines = [
            user_record(ts(), "m1", "go"),
            assistant_record(ts(), "m2",
                             tool_use=("call_x", "Agent",
                                       {"subagent_type": "code"}),
                             request_id="req-1", parent="m1"),
        ]
        subagent = [
            # an assistant record comes first: it is never the brief —
            # the first USER record is picked even from a later position
            assistant_record(ts(), "s0", text="working on it"),
            user_record(ts(), "s1", "alpha-flow investigate the bug",
                        parent="s0", parent_tool="call_x"),
            assistant_record(ts(), "s2", text="found it", request_id="req-p1",
                             parent="s1"),
            user_record(ts(), "s3", "second question", parent="s2"),
        ]
        document = build_audit_document(
            self.load_lines(main_lines, subagents={"probe": subagent}), "qoder"
        )
        dispatch = document.dispatches[0]
        items_by_id = {i.item_id: i for i in document.items}

        brief = items_by_id[dispatch.brief_item_id]
        self.assertEqual(
            (brief.kind, brief.bucket, brief.channel),
            ("dispatch_brief", "inject", "qoder:user"),
        )
        self.assertEqual(brief.preview, "alpha-flow investigate the bug")
        followers = [
            i for i in document.items
            if i.agent_id == "subagent:probe" and i.kind == "user_message"
        ]
        self.assertEqual([i.preview for i in followers],
                         ["second question"])
        self.assertEqual(followers[0].bucket, "user")
        self.assertNotEqual(followers[0].item_id, brief.item_id)

    def test_signature_brief_keeps_skill_body_and_is_tier_a(self):
        ts = _clock()
        sub_records = [
            assistant_record(ts(), "s2", text="ok", request_id="req-p1",
                             parent="s1"),
        ]
        document = build_audit_document(
            single_dispatch_session(
                self,
                '<skill_content name="demo">demonstration body</skill_content>',
                sub_records,
            ),
            "qoder",
        )
        dispatch = document.dispatches[0]
        items_by_id = {i.item_id: i for i in document.items}
        brief = items_by_id[dispatch.brief_item_id]

        # A signature brief keeps its FACT classification — no re-bucketing.
        self.assertEqual(brief.kind, "skill_body")
        self.assertEqual(brief.bucket, "skill")
        self.assertIs(brief, items_by_id[dispatch.brief_item_id])
        self.assertEqual(dispatch.phase_id, "demo")
        self.assertEqual(document.coverage.phase_recognition["tierA"], 1)

        self.assertEqual(len(document.phases), 1)
        phase = document.phases[0]
        self.assertEqual(phase.phase_id, "demo")
        self.assertEqual(phase.name, brief.name)
        self.assertEqual(len(phase.observations), 1)
        observation = phase.observations[0]
        self.assertEqual(observation.kind, "dispatch")
        self.assertEqual(observation.dispatch_id, dispatch.dispatch_id)
        self.assertEqual(observation.tokens_est, dispatch.brief_tokens_est)
        self.assertEqual(observation.channel, "qoder:signature:skill_body")
        self.assertEqual(brief.phase_id, "demo")

    def test_ir_version_is_1_4(self):
        self.assertEqual(IR_VERSION, "1.4")
        self.assertEqual(audit(self).ir_version, "1.4")


class PhaseTierTest(IREventsTestCase, unittest.TestCase):
    def test_brief_text_match_is_tier_b_with_a_recorded_rule_set_version(self):
        (dispatches, phases, _, recognition), _ = project_dispatches(
            single_dispatch_session(self, "alpha-flow investigate the bug"),
            FIXTURE_RULES, "fixture-r1",
        )

        self.assertEqual(
            (recognition["tierA"], recognition["tierB"], recognition["tierC"]),
            (0, 1, 0),
        )
        self.assertEqual(recognition["rule_set_version"], "fixture-r1")
        dispatch = dispatches[0]
        self.assertEqual(dispatch.phase_id, "phase-alpha")
        self.assertEqual(len(dispatch.phase_refs), 1)
        ref = dispatch.phase_refs[0]
        self.assertEqual(
            (ref["ref"], ref["kind"], ref["source"], ref["phase_id"]),
            ("alpha-flow", "unknown", "brief", "phase-alpha"),
        )
        self.assertEqual([p.phase_id for p in phases], ["phase-alpha"])
        self.assertEqual(phases[0].name, "Alpha phase")
        self.assertEqual(phases[0].observations[0].tokens_est,
                         dispatch.brief_tokens_est)

    def test_read_path_match_is_tier_b_and_stamps_the_read_item(self):
        ts = _clock()
        sub_records = [
            assistant_record(ts(), "s2",
                             tool_use=("rd1", "Read",
                                       {"file_path": "/skills/alpha/SKILL.md"}),
                             request_id="req-p1", parent="s1"),
        ]
        (dispatches, phases, _, recognition), items = project_dispatches(
            single_dispatch_session(self, "please inspect", sub_records),
            FIXTURE_RULES, "fixture-r1",
        )

        self.assertEqual(
            (recognition["tierA"], recognition["tierB"], recognition["tierC"]),
            (0, 1, 0),
        )
        dispatch = dispatches[0]
        self.assertEqual(dispatch.phase_id, "phase-alpha")
        kinds = {(r["kind"], r["source"]) for r in dispatch.phase_refs}
        self.assertEqual(
            kinds, {("skill_file", "read:/skills/alpha/SKILL.md")}
        )
        self.assertEqual([p.phase_id for p in phases], ["phase-alpha"])
        # the tier B phase link lands on the Read tool_call item itself
        read_items = [
            i for i in items
            if i.kind == "tool_call" and (i.name or "").lower() == "read"
        ]
        self.assertEqual(len(read_items), 1)
        self.assertEqual(read_items[0].phase_id, "phase-alpha")

    def test_multi_ref_brief_withholds_phase_id_and_observation_cost(self):
        (dispatches, phases, _, recognition), _ = project_dispatches(
            single_dispatch_session(self, "alpha-flow then beta-flow"),
            FIXTURE_RULES, "fixture-r1",
        )

        self.assertEqual(
            (recognition["tierA"], recognition["tierB"], recognition["tierC"]),
            (0, 1, 0),
        )
        dispatch = dispatches[0]
        # evidence recorded, attribution withheld — never guessed between phases
        self.assertIsNone(dispatch.phase_id)
        self.assertIsNotNone(dispatch.brief_tokens_est)
        self.assertEqual(
            {(r["ref"], r["phase_id"]) for r in dispatch.phase_refs},
            {("alpha-flow", "phase-alpha"), ("beta-flow", "phase-beta")},
        )
        self.assertEqual(
            sorted(p.phase_id for p in phases),
            ["phase-alpha", "phase-beta"],
        )
        for phase in phases:
            self.assertEqual(len(phase.observations), 1)
            self.assertIsNone(phase.observations[0].tokens_est)

    def test_no_match_is_tier_c_without_refs_or_phases(self):
        (dispatches, phases, _, recognition), _ = project_dispatches(
            single_dispatch_session(self, "nothing recognizable here"),
            FIXTURE_RULES, "fixture-r1",
        )

        self.assertEqual(
            (recognition["tierA"], recognition["tierB"], recognition["tierC"]),
            (0, 0, 1),
        )
        self.assertIsNone(dispatches[0].phase_id)
        self.assertEqual(dispatches[0].phase_refs, [])
        self.assertEqual(phases, [])

    def test_empty_rule_set_sends_every_non_signature_dispatch_to_tier_c(self):
        (dispatches, phases, _, recognition), _ = project_dispatches(
            single_dispatch_session(self, "alpha-flow investigate the bug"),
            [], None,
        )

        self.assertEqual(len(dispatches), 1)
        self.assertIsNone(dispatches[0].phase_id)
        self.assertEqual(dispatches[0].phase_refs, [])
        self.assertEqual(phases, [])
        self.assertEqual(
            (recognition["tierA"], recognition["tierB"], recognition["tierC"]),
            (0, 0, 1),
        )
        self.assertIsNone(recognition["rule_set_version"])

    def test_builder_runs_with_the_shipped_empty_rule_set(self):
        document = audit(self)
        recognition = document.coverage.phase_recognition
        self.assertEqual(
            recognition["tierA"] + recognition["tierB"] + recognition["tierC"],
            len(document.dispatches),
        )
        self.assertEqual(recognition["tierB"], 0)
        self.assertIsNone(recognition["rule_set_version"])
        self.assertTrue(recognition["corpus_note"])


if __name__ == "__main__":
    unittest.main()
