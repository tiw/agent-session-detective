"""D1: bare-id → namespaced-twin identity merges (IR 1.2)."""

import copy
import unittest
from pathlib import Path

from agent_session_detective.ir.builder import build_audit_document
from agent_session_detective.ir.schema import Observation, SkillEntity
from agent_session_detective.ir.skills import merge_skill_identities
from agent_session_detective.wire import load_session

FIXTURES = Path(__file__).parent / "fixtures" / "ir"


def obs(kind, item_id, *, ts=None, channel="test", tokens=10, agent="main",
        sha1=None):
    return Observation(kind=kind, channel=channel, agent_id=agent,
                       call_id=None, ts=ts, tokens_est=tokens,
                       body_sha1=sha1, item_id=item_id)


def entity(skill_id, name, observations):
    return SkillEntity(skill_id=skill_id, name=name,
                       observations=observations)


class MergeTests(unittest.TestCase):
    def test_bare_id_merges_into_unique_namespaced_twin(self):
        target = entity("pstack:poteto-mode", "pstack:poteto-mode",
                        [obs("execution", "main:1:0", ts=10.0),
                         obs("stub", "main:2:0", ts=11.0)])
        bare = entity("poteto-mode", "poteto-mode",
                      [obs("body", "main:3:0", ts=12.0)])

        merged, report = merge_skill_identities([target, bare],
                                                {"something-else"})

        self.assertEqual([e.skill_id for e in merged],
                         ["pstack:poteto-mode"])
        entry = merged[0]
        self.assertEqual([o.kind for o in entry.observations],
                         ["execution", "stub", "body"])
        self.assertEqual([o.item_id for o in entry.observations],
                         ["main:1:0", "main:2:0", "main:3:0"])
        self.assertEqual([o.raw_skill_id for o in entry.observations],
                         [None, None, "poteto-mode"])
        self.assertEqual(entry.aliases, ["poteto-mode"])
        self.assertEqual(report, {
            "merges": 1,
            "aliases": {"pstack:poteto-mode": ["poteto-mode"]},
            "ambiguous": [],
            "note": ("bare ids merge into a unique namespaced twin unless they "
                     "have a listing of their own; listed or ambiguous ids stay "
                     "unmerged"),
        })

    def test_name_resolution_keeps_namespaced_name_when_it_differs(self):
        target = entity("pstack:poteto-mode", "Poteto Mode",
                        [obs("stub", "main:1:0", ts=10.0)])
        bare = entity("poteto-mode", "poteto-mode",
                      [obs("body", "main:2:0", ts=11.0)])

        merged, _ = merge_skill_identities([target, bare], set())

        self.assertEqual(merged[0].name, "Poteto Mode")

    def test_name_resolution_falls_back_to_bare_id(self):
        target = entity("ns:demo", "ns:demo",
                        [obs("execution", "main:1:0", ts=10.0)])
        bare = entity("demo", "demo", [obs("body", "main:2:0", ts=11.0)])

        merged, _ = merge_skill_identities([target, bare], set())

        self.assertEqual(merged[0].name, "demo")

    def test_name_resolution_prefers_bare_display_name(self):
        target = entity("ns:demo", "ns:demo",
                        [obs("execution", "main:1:0", ts=10.0)])
        bare = entity("demo", "Demo Skill", [obs("body", "main:2:0", ts=11.0)])

        merged, _ = merge_skill_identities([target, bare], set())

        self.assertEqual(merged[0].name, "Demo Skill")

    def test_own_listing_guard_keeps_the_bare_entity(self):
        target = entity("presentations:pptx", "presentations:pptx",
                        [obs("execution", "main:1:0", ts=10.0)])
        bare = entity("pptx", "pptx", [
            obs("listing", "main:2:0", ts=11.0, channel="skill_listing",
                tokens=5),
            obs("body", "main:3:0", ts=12.0)])

        merged, report = merge_skill_identities([target, bare], {"pptx"})

        self.assertEqual([e.skill_id for e in merged],
                         ["presentations:pptx", "pptx"])
        self.assertEqual(report["merges"], 0)
        self.assertEqual(merged[0].aliases, [])

    def test_listing_set_alone_blocks_the_merge(self):
        target = entity("ns:demo", "ns:demo",
                        [obs("execution", "main:1:0", ts=10.0)])
        bare = entity("demo", "demo", [obs("body", "main:2:0", ts=11.0)])

        merged, report = merge_skill_identities([target, bare], {"demo"})

        self.assertEqual([e.skill_id for e in merged], ["ns:demo", "demo"])
        self.assertEqual(report["merges"], 0)

    def test_observation_listing_guard_keeps_the_bare_entity(self):
        target = entity("ns:demo", "ns:demo",
                        [obs("execution", "main:1:0", ts=10.0)])
        bare = entity("demo", "demo", [
            obs("listing", "main:2:0", ts=11.0, channel="skill_listing",
                tokens=5)])

        merged, report = merge_skill_identities([target, bare], set())

        self.assertEqual([e.skill_id for e in merged], ["ns:demo", "demo"])
        self.assertEqual(report["merges"], 0)

    def test_two_candidates_are_ambiguous_and_stay_unmerged(self):
        a = entity("a:dup", "a:dup", [obs("stub", "main:1:0", ts=10.0)])
        b = entity("b:dup", "b:dup", [obs("stub", "main:2:0", ts=11.0)])
        bare = entity("dup", "dup", [obs("body", "main:3:0", ts=12.0)])

        merged, report = merge_skill_identities([a, b, bare], set())

        self.assertEqual([e.skill_id for e in merged],
                         ["a:dup", "b:dup", "dup"])
        self.assertEqual(report["merges"], 0)
        self.assertEqual(report["ambiguous"], ["dup"])

    def test_unrelated_bare_ids_are_untouched(self):
        lonely = entity("lonely", "lonely", [obs("body", "main:1:0", ts=10.0)])

        merged, report = merge_skill_identities([lonely], set())

        self.assertEqual([e.skill_id for e in merged], ["lonely"])
        self.assertEqual(report["merges"], 0)
        self.assertEqual(report["ambiguous"], [])

    def test_colon_bearing_ids_are_never_merge_sources(self):
        target = entity("x:ns:demo", "x:ns:demo",
                        [obs("stub", "main:1:0", ts=10.0)])
        bare = entity("ns:demo", "ns:demo",
                      [obs("body", "main:2:0", ts=11.0)])

        merged, report = merge_skill_identities([target, bare], set())

        self.assertEqual([e.skill_id for e in merged],
                         ["x:ns:demo", "ns:demo"])
        self.assertEqual(report["merges"], 0)

    def test_null_ts_observations_sort_last(self):
        target = entity("ns:s", "ns:s", [])
        bare = entity("s", "s", [
            obs("body", "i0", ts=20.0),
            obs("stub", "i1", ts=None),
            obs("execution", "i2", ts=10.0)])

        merged, _ = merge_skill_identities([target, bare], set())

        self.assertEqual([o.item_id for o in merged[0].observations],
                         ["i2", "i0", "i1"])

    def test_inputs_are_not_mutated(self):
        target = entity("ns:demo", "ns:demo",
                        [obs("execution", "main:1:0", ts=10.0)])
        bare = entity("demo", "demo", [obs("body", "main:2:0", ts=11.0)])
        entities = [target, bare]
        listing = {"pptx"}
        snapshot = copy.deepcopy(entities)
        snapshot_listing = copy.deepcopy(listing)

        merge_skill_identities(entities, listing)

        self.assertEqual(entities, snapshot)
        self.assertEqual(listing, snapshot_listing)


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_audit_document(
            load_session(FIXTURES / "skill-identity.jsonl"), "qoder")

    def entities(self):
        return {entity.skill_id: entity for entity in self.document.skills}

    def test_entities_carry_the_merge_and_the_ambiguity(self):
        self.assertEqual(
            [e.skill_id for e in self.document.skills],
            ["a:dup", "b:dup", "dup", "ns:demo", "pptx",
             "presentations:pptx"],
        )
        demo = self.entities()["ns:demo"]
        self.assertEqual(demo.name, "demo")
        self.assertEqual(demo.aliases, ["demo"])
        self.assertEqual([o.kind for o in demo.observations],
                         ["execution", "stub", "body"])
        absorbed = [o for o in demo.observations
                    if o.raw_skill_id is not None]
        self.assertEqual(len(absorbed), 1)
        self.assertEqual(absorbed[0].raw_skill_id, "demo")
        self.assertEqual(absorbed[0].channel, "tool:read")

    def test_coverage_counts_merges_and_ambiguities(self):
        identity = self.document.coverage.skill_identity
        self.assertEqual(identity["merges"], 1)
        self.assertEqual(identity["aliases"], {"ns:demo": ["demo"]})
        self.assertEqual(identity["ambiguous"], ["dup"])

    def test_loads_join_reads_to_stubs_and_keep_unattached_stubs_unavailable(self):
        loads = self.document.skill_loads
        self.assertEqual([row.skill_id for row in loads],
                         ["ns:demo", "a:dup", "b:dup", "dup"])
        first = loads[0]
        self.assertEqual(first.kind, "load")
        self.assertEqual(first.channel, "tool:read")
        self.assertEqual(first.cost_basis, "body")
        self.assertIsNotNone(first.cost_tokens_est)
        for row in loads[1:3]:
            self.assertEqual(row.kind, "load")
            self.assertEqual(row.cost_basis, "unavailable")
            self.assertIsNone(row.cost_tokens_est)
            self.assertIsNone(row.channel)
        last = loads[3]
        self.assertEqual(last.skill_id, "dup")
        self.assertEqual(last.channel, "tool:read")
        self.assertEqual(last.cost_basis, "body")


if __name__ == "__main__":
    unittest.main()
