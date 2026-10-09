# Actual Skill Tree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the actually-executed skill tree (agents → dispatches → per-skill loads, with evidence rows) from real session transcripts: canonical skill identity (D1), a deterministic tree analysis (D2), a self-contained HTML page (D3), and a CLI flag (D4).

**Architecture:** Facts stay in the IR. D1 merges a bare skill id into its unique namespaced twin inside the builder, *before* load pairing, so every load row and cost attaches to the canonical entity; merged observations keep their recorded (raw) id as provenance. D2 (`ir/skill_tree.py`) derives a pure `{nodes, edges, loose, totals}` structure from the documented facts only — tree edges join via dispatch rows, never via the Qoder hardcoded `parent_id`. D3 (`tree_html.py`) renders that structure as one read-only HTML page with no JavaScript and no external assets; every number is counted from the document, EST-labelled, or the literal word `unavailable` — a stub-derived cost is never invented. D4 exposes the page via `--tree-out`.

**Tech Stack:** Python ≥3.9 stdlib only (dataclasses, `html`, `re`, `datetime`); `unittest.TestCase` tests run with `PYTHONPATH=src python3 -m pytest`; deterministic string assembly for HTML (same rules as `report.py`).

---

## File Structure

Create:

- `tests/fixtures/ir/skill-identity.jsonl` — 13-record Qoder transcript: two namespaced skills with a bare-id body each (`ns:demo` ← `demo` merge; `a:dup`/`b:dup` ambiguity).
- `tests/test_skill_identity.py` — D1 unit tests (merge rules) + fixture end-to-end assertions.
- `tests/test_skill_tree.py` — D2 tree analysis contract tests.
- `tests/test_tree_html.py` — D3 renderer contract tests.
- `tests/test_cli_tree.py` — D4 `--tree-out` test.
- `src/agent_session_detective/ir/skill_tree.py` — D2 tree analysis (pure, no I/O).
- `src/agent_session_detective/tree_html.py` — D3 renderer (pure string assembly).

Modify:

- `src/agent_session_detective/ir/schema.py` — IR 1.2: `IR_VERSION`, `Observation.raw_skill_id`, `SkillEntity.aliases`, `CoverageReport.skill_identity`.
- `src/agent_session_detective/ir/skills.py` — `merge_skill_identities` + helpers.
- `src/agent_session_detective/ir/builder.py` — run the merge between `build_skills` and `build_loads`; pass the report into `build_coverage`.
- `src/agent_session_detective/ir/coverage.py` — carry the `skill_identity` block.
- `src/agent_session_detective/ir/analyses.py` — sixth analysis `skill_tree`.
- `src/agent_session_detective/cli.py` — `--tree-out`.
- `tests/test_ir_schema.py`, `tests/test_ir_builder.py`, `tests/test_ir_dispatch.py`, `tests/test_cli.py` — IR version goldens (`1.1` → `1.2`).
- `tests/test_ir_properties.py`, `tests/test_ir_analyses.py` — fixture registry + the load-row body-kind contract.
- `tests/ir_helpers.py` — accept merged-away raw ids in the item/entity join check.

Do **not** touch (uncommitted, out of scope): `src/agent_session_detective/report.py` (read-only source for the CSS copy), `src/agent_session_detective/webapp/app.js`, `tests/test_wire.py`.

Test budget (cumulative, all from `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`): 210 baseline → 221 after Task 1 → 224 after Task 2 → 232 after Task 3 → 242 after Task 4 → 243 after Task 5.

---

### Task 1: IR 1.2 schema + D1 identity merge (unit level)

**Files:**
- Modify: `src/agent_session_detective/ir/schema.py`
- Modify: `src/agent_session_detective/ir/skills.py`
- Create: `tests/test_skill_identity.py`
- Modify: `tests/test_ir_schema.py:122,133`, `tests/test_ir_builder.py:249`, `tests/test_ir_dispatch.py:412-414`, `tests/test_cli.py:172`

- [ ] **Step 1: Write the failing merge unit tests**

Create `tests/test_skill_identity.py`:

```python
"""D1: bare-id → namespaced-twin identity merges (IR 1.2)."""

import copy
import unittest

from agent_session_detective.ir.schema import Observation, SkillEntity
from agent_session_detective.ir.skills import merge_skill_identities


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_skill_identity.py -q`

Expected: collection error — `ImportError: cannot import name 'merge_skill_identities' from 'agent_session_detective.ir.skills'`.

- [ ] **Step 3: Add the IR 1.2 schema fields**

In `src/agent_session_detective/ir/schema.py`:

Bump the version (line 15):

```python
IR_VERSION = "1.2"
```

Append a field to `Observation` (after `item_id: str`):

```python
    # Identity merge (IR 1.2): the id this observation was recorded under
    # before a bare id merged into its namespaced twin; null = canonical.
    raw_skill_id: Optional[str] = None
```

Append a field to `SkillEntity` (after `observations: List[Observation]`):

```python
    # Identity merge (IR 1.2): bare ids absorbed into this entity, sorted.
    aliases: List[str] = field(default_factory=list)
```

Append a field to `CoverageReport` (after `phase_recognition: dict = field(default_factory=dict)`):

```python
    # IR 1.2: bare-id → namespaced-twin merges, ambiguities counted.
    skill_identity: dict = field(default_factory=dict)
```

- [ ] **Step 4: Flip the IR version goldens (4 files, 6 sites)**

- `tests/test_cli.py:172`: `self.assertEqual(document["ir_version"], "1.1")` → `"1.2"`
- `tests/test_ir_builder.py:249`: `self.assertEqual(document.ir_version, "1.1")` → `"1.2"`
- `tests/test_ir_dispatch.py:412-414`: rename `def test_ir_version_is_1_1(self):` → `def test_ir_version_is_1_2(self):`; both `"1.1"` asserts → `"1.2"`
- `tests/test_ir_schema.py:122`: `self.assertEqual(IR_VERSION, "1.1")` → `"1.2"`; `:133`: `self.assertEqual(revived["ir_version"], "1.1")` → `"1.2"`

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`

Expected: `210 passed` (schema fields are additive; only the version goldens moved).

- [ ] **Step 5: Implement the merge**

In `src/agent_session_detective/ir/skills.py`, extend the imports:

```python
from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Set, Tuple
```

Append to the end of the file (after `build_skills`):

```python
def _has_own_listing(entity: SkillEntity, listing_skill_ids: Set[str]) -> bool:
    if entity.skill_id in listing_skill_ids:
        return True
    return any(observation.kind == "listing"
               for observation in entity.observations)


def _merged_name(target: SkillEntity, absorbed: SkillEntity) -> str:
    if target.name != target.skill_id:
        return target.name
    if absorbed.name != absorbed.skill_id:
        return absorbed.name
    return absorbed.skill_id


def merge_skill_identities(
    entities: List[SkillEntity],
    listing_skill_ids: Set[str],
) -> Tuple[List[SkillEntity], dict]:
    """D1: one bare id merges into its unique ``:``-suffixed twin.

    A bare id stays unmerged when it has a listing of its own (it is a
    first-class skill) or when more than one other entity id ends with
    ``":" + bare`` (ambiguous — counted, never guessed). Absorbed
    observations keep ``raw_skill_id`` as provenance; the inputs are never
    mutated and item ids are never re-keyed.
    """
    absorbed_by_target: Dict[str, List[SkillEntity]] = {}
    absorbed_ids: Set[str] = set()
    ambiguous: List[str] = []
    for entity in entities:
        bare = entity.skill_id
        if ":" in bare or _has_own_listing(entity, listing_skill_ids):
            continue
        candidates = [other for other in entities
                      if other.skill_id.endswith(":" + bare)]
        if len(candidates) > 1:
            ambiguous.append(bare)
            continue
        if candidates:
            absorbed_by_target.setdefault(
                candidates[0].skill_id, []).append(entity)
            absorbed_ids.add(bare)
    merged_entities: List[SkillEntity] = []
    aliases: Dict[str, List[str]] = {}
    for entity in entities:
        if entity.skill_id in absorbed_ids:
            continue
        absorbed = absorbed_by_target.get(entity.skill_id)
        if not absorbed:
            merged_entities.append(entity)
            continue
        observations = list(entity.observations)
        for source in absorbed:
            observations.extend(
                replace(observation, raw_skill_id=source.skill_id)
                for observation in source.observations)
        observations.sort(key=lambda observation: (
            observation.ts is None, observation.ts or 0.0,
            observation.item_id, observation.kind))
        name = entity.name
        for source in absorbed:
            name = _merged_name(entity, source)
        merged_aliases = sorted(
            list(entity.aliases) + [source.skill_id for source in absorbed])
        aliases[entity.skill_id] = merged_aliases
        merged_entities.append(replace(
            entity, name=name, observations=observations,
            aliases=merged_aliases))
    merges = sum(len(a) for a in absorbed_by_target.values())
    report = {
        "merges": merges,
        "aliases": aliases,
        "ambiguous": sorted(ambiguous),
        "note": ("bare ids merge into a unique namespaced twin unless they "
                 "have a listing of their own; listed or ambiguous ids stay "
                 "unmerged"),
    }
    return merged_entities, report
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_skill_identity.py -q`

Expected: `11 passed`.

- [ ] **Step 7: Run the full suite**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`

Expected: `221 passed`.

- [ ] **Step 8: Commit**

```bash
git add src/agent_session_detective/ir/schema.py src/agent_session_detective/ir/skills.py tests/test_skill_identity.py tests/test_ir_schema.py tests/test_ir_builder.py tests/test_ir_dispatch.py tests/test_cli.py
git commit -m "feat: merge bare skill ids into their namespaced twins (IR 1.2)"
```

---

### Task 2: Wire the merge into the builder with an end-to-end fixture

**Files:**
- Create: `tests/fixtures/ir/skill-identity.jsonl`
- Modify: `tests/test_skill_identity.py` (append `EndToEndTests`)
- Modify: `src/agent_session_detective/ir/builder.py` (import at line 50; merge between `build_skills` and `build_loads` at lines 308-313; `build_coverage` call at line 345)
- Modify: `src/agent_session_detective/ir/coverage.py` (keyword-only parameter + constructor kwargs, lines 59-61 and 92-94)
- Modify: `tests/test_ir_properties.py` (two registries + `EvidencePropertiesTest` body-kind/alias fix)
- Modify: `tests/test_ir_analyses.py` (fixture registry)
- Modify: `tests/ir_helpers.py` (`skill_ids` union with merged-away raw ids)

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/ir/skill-identity.jsonl` with exactly these 13 lines:

```jsonl
{"type": "user", "uuid": "u1", "parentUuid": null, "isSidechain": false, "timestamp": "2026-10-08T12:00:00.000Z", "message": {"content": [{"type": "text", "text": "audit this"}]}}
{"type": "attachment", "uuid": "u2", "parentUuid": "u1", "timestamp": "2026-10-08T12:00:01.000Z", "attachment": {"type": "skill_listing", "content": "- pptx: PPTX presentations\n- presentations:pptx: PPTX plugin"}}
{"type": "assistant", "uuid": "u3", "parentUuid": "u2", "isSidechain": false, "timestamp": "2026-10-08T12:00:02.000Z", "requestTokenAnchor": {"request": "a0a0a0a0a0a0a0a0", "response": "a1a1a1a1a1a1a1a1", "requestId": "req-1"}, "message": {"model": "qoder-pro", "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "ns:demo"}}], "usage": {"input_tokens": 1000, "output_tokens": 10}}}
{"type": "user", "uuid": "u4", "parentUuid": "u3", "isSidechain": false, "timestamp": "2026-10-08T12:00:03.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Launching skill: ns:demo"}]}}
{"type": "assistant", "uuid": "u5", "parentUuid": "u4", "isSidechain": false, "timestamp": "2026-10-08T12:00:04.000Z", "requestTokenAnchor": {"request": "b0b0b0b0b0b0b0b0", "response": "b1b1b1b1b1b1b1b1", "requestId": "req-2"}, "message": {"model": "qoder-pro", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/plugins/pstack/skills/demo/SKILL.md"}}], "usage": {"input_tokens": 1100, "output_tokens": 10}}}
{"type": "user", "uuid": "u6", "parentUuid": "u5", "isSidechain": false, "timestamp": "2026-10-08T12:00:05.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "Demo skill body via read."}]}}
{"type": "assistant", "uuid": "u7", "parentUuid": "u6", "isSidechain": false, "timestamp": "2026-10-08T12:00:06.000Z", "requestTokenAnchor": {"request": "c0c0c0c0c0c0c0c0", "response": "c1c1c1c1c1c1c1c1", "requestId": "req-3"}, "message": {"model": "qoder-pro", "content": [{"type": "tool_use", "id": "t3", "name": "Skill", "input": {"skill": "a:dup"}}], "usage": {"input_tokens": 1200, "output_tokens": 10}}}
{"type": "user", "uuid": "u8", "parentUuid": "u7", "isSidechain": false, "timestamp": "2026-10-08T12:00:07.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "Launching skill: a:dup"}]}}
{"type": "assistant", "uuid": "u9", "parentUuid": "u8", "isSidechain": false, "timestamp": "2026-10-08T12:00:08.000Z", "requestTokenAnchor": {"request": "d0d0d0d0d0d0d0d0", "response": "d1d1d1d1d1d1d1d1", "requestId": "req-4"}, "message": {"model": "qoder-pro", "content": [{"type": "tool_use", "id": "t4", "name": "Skill", "input": {"skill": "b:dup"}}], "usage": {"input_tokens": 1300, "output_tokens": 10}}}
{"type": "user", "uuid": "u10", "parentUuid": "u9", "isSidechain": false, "timestamp": "2026-10-08T12:00:09.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t4", "content": "Launching skill: b:dup"}]}}
{"type": "assistant", "uuid": "u11", "parentUuid": "u10", "isSidechain": false, "timestamp": "2026-10-08T12:00:10.000Z", "requestTokenAnchor": {"request": "e0e0e0e0e0e0e0e0", "response": "e1e1e1e1e1e1e1e1", "requestId": "req-5"}, "message": {"model": "qoder-pro", "content": [{"type": "tool_use", "id": "t5", "name": "Read", "input": {"file_path": "/skills/dup/SKILL.md"}}], "usage": {"input_tokens": 1400, "output_tokens": 10}}}
{"type": "user", "uuid": "u12", "parentUuid": "u11", "isSidechain": false, "timestamp": "2026-10-08T12:00:11.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t5", "content": "Dup body via read."}]}}
{"type": "assistant", "uuid": "u13", "parentUuid": "u12", "isSidechain": false, "timestamp": "2026-10-08T12:00:12.000Z", "requestTokenAnchor": {"request": "f0f0f0f0f0f0f0f0", "response": "f1f1f1f1f1f1f1f1", "requestId": "req-6"}, "message": {"model": "qoder-pro", "content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": 1500, "output_tokens": 10}}}
```

What this fixture exercises: a namespaced `Skill` execution (`ns:demo`) whose body arrives through a bare `Read` path (`/plugins/pstack/skills/demo/SKILL.md` → bare id `demo`, no own listing → merges); an ambiguous bare id (`dup` has two `:dup` candidates `a:dup`/`b:dup` → stays unmerged, only its own read body); and an own-listing pair (`pptx` + `presentations:pptx`) that must NOT merge.

- [ ] **Step 2: Append the end-to-end tests**

Append to `tests/test_skill_identity.py`:

```python
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
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_skill_identity.py -q`

Expected: `3 failed, 11 passed` — the builder still ships unmerged entities (`demo` still present) and `coverage.skill_identity` is `{}`.

- [ ] **Step 4: Wire the builder and coverage**

In `src/agent_session_detective/ir/builder.py` line 50, change:

```python
from .skills import build_skills
```

to:

```python
from .skills import build_skills, merge_skill_identities
```

Replace the block at lines 308-313:

```python
    skills = build_skills(
        extraction.items, all_calls, seq_ts, extraction.listing_lines
    )
    skill_loads, redundant_bodies = build_loads(
        skills, extraction.items, agents
    )
```

with:

```python
    skills = build_skills(
        extraction.items, all_calls, seq_ts, extraction.listing_lines
    )
    listing_skill_ids = {
        skill_id
        for lines in extraction.listing_lines.values()
        for skill_id, _display, _tokens in lines
    }
    skills, skill_identity = merge_skill_identities(
        skills, listing_skill_ids
    )
    skill_loads, redundant_bodies = build_loads(
        skills, extraction.items, agents
    )
```

In the same file, inside the `build_coverage(...)` call, after `phase_recognition=phase_recognition,` (line 345) add:

```python
        skill_identity=skill_identity,
```

In `src/agent_session_detective/ir/coverage.py`, extend the keyword-only parameters (lines 59-61):

```python
    skill_load_evidence: Optional[dict] = None,
    dispatch_links: Optional[dict] = None,
    phase_recognition: Optional[dict] = None,
    skill_identity: Optional[dict] = None,
) -> CoverageReport:
```

and the constructor kwargs (lines 92-94):

```python
        skill_load_evidence=skill_load_evidence or {},
        dispatch_links=dispatch_links or {},
        phase_recognition=phase_recognition or {},
        skill_identity=skill_identity or {},
    )
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_skill_identity.py -q`

Expected: `14 passed`.

- [ ] **Step 6: Run the full suite — wiring alone must not move any existing golden**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`

Expected: `221 passed` (the new fixture is not in any registry yet; no existing fixture has a bare/twin pair without its own listing, so no merge fires for them).

- [ ] **Step 7: Register the fixture and fix the two collision sites**

`tests/test_ir_properties.py` lines 14-17 — add `skill-identity.jsonl` to `ALL_FIXTURES`:

```python
ALL_FIXTURES = ["tier1.jsonl", "tier2.jsonl", "tier3.jsonl",
                "tier3-usage.jsonl", "compaction.jsonl",
                "reinject.jsonl", "attachments.jsonl", "calibration.jsonl",
                "dispatch.jsonl", "skill-identity.jsonl"]
```

Same file, `CoverageLedgerTest.TIER_COUNTS` — add after the `dispatch.jsonl` entry:

```python
        "skill-identity.jsonl": (1, 0, 0),
```

Same file, `CoverageLedgerTest.CHECKED_CALLS` — add after the `dispatch.jsonl` entry:

```python
        "skill-identity.jsonl": 6,
```

`tests/test_ir_analyses.py` lines 19-21 — add `skill-identity.jsonl` to `ALL_FIXTURES`:

```python
ALL_FIXTURES = ["tier1.jsonl", "tier2.jsonl", "tier3.jsonl",
                "tier3-usage.jsonl", "compaction.jsonl",
                "reinject.jsonl", "attachments.jsonl", "dispatch.jsonl",
                "skill-identity.jsonl"]
```

`tests/ir_helpers.py` line 62 — the merged-away raw id must stay resolvable for items that still carry it:

```python
    skill_ids = {skill.skill_id for skill in document.skills}
    raw_skill_ids = {
        observation.raw_skill_id
        for skill in document.skills
        for observation in skill.observations
        if observation.raw_skill_id is not None
    }
```

and lines 104-106:

```python
    for item in document.items:
        if item.skill_id is not None:
            test.assertIn(item.skill_id, skill_ids | raw_skill_ids)
```

`tests/test_ir_properties.py`, `EvidencePropertiesTest.test_load_rows_resolve_and_costs_come_only_from_body_items` — hoist the skills index next to `items_by_id` (line 195):

```python
            items_by_id = {item.item_id: item for item in document.items}
            skills_by_id = {skill.skill_id: skill for skill in document.skills}
            agent_ids = {agent.agent_id for agent in document.agents}
```

and replace the body-join assertions (lines 208-215):

```python
                else:
                    body = items_by_id[row.body_item_id]
                    expected_kind = (
                        "tool_result" if row.channel == "tool:read"
                        else "skill_body"
                    )
                    self.assertEqual(body.kind, expected_kind, name)
                    if body.skill_id != row.skill_id:
                        self.assertIn(
                            body.skill_id,
                            skills_by_id[row.skill_id].aliases,
                            name,
                        )
                    self.assertEqual(
                        row.cost_tokens_est, body.tokens_est, name
                    )
                    self.assertEqual(row.body_sha1, body.sha1, name)
```

- [ ] **Step 8: Run the full suite**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`

Expected: `224 passed`.

- [ ] **Step 9: Commit**

```bash
git add tests/fixtures/ir/skill-identity.jsonl tests/test_skill_identity.py src/agent_session_detective/ir/builder.py src/agent_session_detective/ir/coverage.py tests/test_ir_properties.py tests/test_ir_analyses.py tests/ir_helpers.py
git commit -m "feat: wire skill identity merges through the builder (IR 1.2)"
```

---

### Task 3: The actual skill tree analysis (D2)

**Files:**
- Create: `src/agent_session_detective/ir/skill_tree.py`
- Create: `tests/test_skill_tree.py`
- Modify: `src/agent_session_detective/ir/analyses.py` (import, docstring bullet, 6th key)
- Modify: `tests/test_cli.py` (two `sorted(analyses)` sites, lines 179-181 and 199-201)
- Modify: `tests/test_ir_analyses.py` (`sorted(analyses)` site, lines 247-249)

- [ ] **Step 1: Write the failing analysis tests**

Create `tests/test_skill_tree.py`:

```python
"""Golden tests for the actual skill tree analysis (D2)."""

import unittest

from agent_session_detective.ir.schema import (
    AuditDocument, ContextAgent, CoverageReport, Dispatch, LoadEvidence,
    Observation, SkillEntity)
from agent_session_detective.ir.skill_tree import skill_tree

MAIN = "main"
CHILD = "subagent:agent-apstack:poteto-agent-241caed765ad6b60"


def agent(agent_id, request_ids=("main:0",), model="qoder-pro",
          context_window=200000):
    return ContextAgent(agent_id=agent_id, parent_id=None,
                        origin_channel="qoder", model=model,
                        context_window=context_window,
                        request_ids=list(request_ids), active_leaf=None)


def observation(kind, agent_id=MAIN, *, ts=None, channel="tool:read",
                tokens=10, sha1=None, item_id="i0", raw_skill_id=None):
    return Observation(kind=kind, channel=channel, agent_id=agent_id,
                       call_id=None, ts=ts, tokens_est=tokens,
                       body_sha1=sha1, item_id=item_id,
                       raw_skill_id=raw_skill_id)


def skill(skill_id, observations, name=None, aliases=()):
    return SkillEntity(skill_id=skill_id, name=name or skill_id,
                       observations=list(observations),
                       aliases=list(aliases))


def load(load_id, agent_id, skill_id, *, kind="load", channel="tool:read",
         cost=100, basis="body", sha1=None):
    return LoadEvidence(load_id=load_id, agent_id=agent_id, skill_id=skill_id,
                        kind=kind, marker_item_id=None, body_item_id=None,
                        body_sha1=sha1, channel=channel, ts=1000.0,
                        cost_tokens_est=cost, cost_basis=basis)


def dispatch(dispatch_id, parent, child, subagent_type,
             description="do a thing", brief=50):
    return Dispatch(dispatch_id=dispatch_id, parent_agent_id=parent,
                    tool_use_id="t1", tool_item_id="i1",
                    subagent_agent_id=child, brief_item_id=None,
                    brief_tokens_est=brief, subagent_type=subagent_type,
                    description=description, meta=None, phase_refs=[],
                    phase_id=None)


def document(agents, skills=(), loads=(), dispatches=()):
    return AuditDocument(
        adapter={"id": "test", "version": "test"},
        estimator_version="test",
        source_files=["test.jsonl"],
        agents=list(agents),
        requests=[],
        items=[],
        skills=list(skills),
        compactions=[],
        coverage=CoverageReport(
            per_field={}, requests_with_anchor="0/0", request_identity={},
            bucket_sources={}, unknown_channels=[], dropped_records={},
            notes=[]),
        skill_loads=list(loads),
        dispatches=list(dispatches),
    )


def node_by_id(tree, agent_id):
    return next(node for node in tree["nodes"] if node["agent_id"] == agent_id)


class TreeShapeTest(unittest.TestCase):
    def test_nodes_edges_and_labels(self):
        tree = skill_tree(document(
            agents=[agent(MAIN, request_ids=("main:0", "main:1")),
                    agent(CHILD, request_ids=("subagent:...:0",))],
            dispatches=[dispatch("main:dispatch:1", MAIN, CHILD,
                                 "pstack:poteto-agent")],
        ))
        child = node_by_id(tree, CHILD)
        self.assertEqual(child["label"], "pstack:poteto-agent · 241caed7")
        self.assertEqual(child["parent_agent_id"], MAIN)
        self.assertEqual(child["via_dispatch_id"], "main:dispatch:1")
        self.assertEqual(child["n_requests"], 1)
        main = node_by_id(tree, MAIN)
        self.assertEqual(main["label"], "main")
        self.assertEqual(main["n_requests"], 2)
        self.assertEqual(tree["loose"], [])
        self.assertEqual(tree["edges"], [{
            "dispatch_id": "main:dispatch:1",
            "from_agent_id": MAIN,
            "to_agent_id": CHILD,
            "subagent_type": "pstack:poteto-agent",
            "description": "do a thing",
            "brief_tokens_est": 50,
            "phase_id": None,
        }])
        self.assertEqual(tree["totals"]["agents"], 2)
        self.assertEqual(tree["totals"]["edges"], 1)

    def test_label_falls_back_to_agent_id_without_dispatch_type(self):
        unknown = "subagent:agent-x-abcdef1234567890"
        tree = skill_tree(document(agents=[agent(MAIN), agent(unknown)]))
        self.assertEqual(node_by_id(tree, unknown)["label"], unknown)

    def test_orphan_dispatch_and_loose_agent(self):
        tree = skill_tree(document(
            agents=[agent(MAIN), agent(CHILD)],
            dispatches=[dispatch("main:dispatch:1", MAIN, None,
                                 "pstack:poteto-agent")],
        ))
        self.assertEqual(len(tree["edges"]), 1)
        self.assertIsNone(tree["edges"][0]["to_agent_id"])
        self.assertEqual(tree["totals"]["edges"], 0)
        self.assertEqual(tree["loose"], [{
            "agent_id": CHILD,
            "label": CHILD,
            "reason": "no incoming dispatch edge",
        }])

    def test_document_without_main_has_no_loose(self):
        other = "subagent:agent-a-1111222233334444"
        tree = skill_tree(document(agents=[agent(other), agent(CHILD)]))
        self.assertEqual(tree["loose"], [])


class AttachmentTest(unittest.TestCase):
    def test_attachments_exclude_listing_only_skills(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[skill("demo", [
                observation("listing", channel="skill_listing", tokens=500,
                            item_id="i1")])],
        ))
        node = node_by_id(tree, MAIN)
        self.assertEqual(node["attachments"], [])
        self.assertEqual(node["ambient"],
                         {"skills": 1, "injections": 1, "tokens_est": 500})

    def test_execution_marks_the_attachment(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[skill("pstack:poteto-mode", [
                observation("execution", channel="skill_tool_call", ts=1.0,
                            tokens=3, item_id="i1"),
                observation("stub", channel="tool_result", ts=2.0, tokens=9,
                            item_id="i2"),
                observation("body", channel="tool:read", ts=3.0, tokens=357,
                            item_id="i3", sha1="abc123")],
                name="poteto-mode", aliases=("poteto-mode",))],
            loads=[load("main:load:1", MAIN, "pstack:poteto-mode", cost=357,
                        sha1="abc123")],
        ))
        attachment = node_by_id(tree, MAIN)["attachments"][0]
        self.assertTrue(attachment["executed"])
        self.assertEqual(attachment["loads_tokens_est"], 357)
        self.assertEqual(attachment["channels"], ["tool:read"])
        self.assertEqual(attachment["sha1s"], ["abc123"])
        self.assertEqual(attachment["name"], "poteto-mode")
        self.assertEqual(attachment["aliases"], ["poteto-mode"])

    def test_null_cost_load_is_kept_and_counted(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("stub", ts=1.0)])],
            loads=[load("main:load:1", MAIN, "demo", channel=None, cost=None,
                        basis="unavailable")],
        ))
        attachment = node_by_id(tree, MAIN)["attachments"][0]
        row = attachment["loads"][0]
        self.assertEqual(row["cost_basis"], "unavailable")
        self.assertIsNone(row["cost_tokens_est"])
        self.assertIsNone(row["channel"])
        self.assertEqual(attachment["loads_tokens_est"], 0)
        self.assertEqual(tree["totals"]["unavailable"], 1)


class TotalsTest(unittest.TestCase):
    def test_totals_are_re_derived(self):
        tree = skill_tree(document(
            agents=[agent(MAIN)],
            skills=[
                skill("a:x", [observation("listing", channel="skill_listing",
                                          tokens=11, item_id="i1")]),
                skill("a:y", [observation("body", tokens=5, item_id="i2")]),
            ],
            loads=[
                load("main:load:1", MAIN, "a:x", cost=100),
                load("main:load:2", MAIN, "a:y", channel=None, cost=None,
                     basis="unavailable"),
                load("main:load:3", MAIN, "a:x", kind="reload", cost=100),
            ],
        ))
        self.assertEqual(tree["totals"], {
            "agents": 1,
            "edges": 0,
            "loads": 2,
            "reloads": 1,
            "unavailable": 1,
            "executions": 0,
            "ambient_tokens_est": 11,
            "attached_tokens_est": 200,
        })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_skill_tree.py -q`

Expected: collection error — `ModuleNotFoundError: No module named 'agent_session_detective.ir.skill_tree'`.

- [ ] **Step 3: Write the tree analysis**

Create `src/agent_session_detective/ir/skill_tree.py`:

```python
# src/agent_session_detective/ir/skill_tree.py
"""The actual skill tree: agents as nodes, dispatch rows as edges.

Built from the document alone (loads, dispatches, skills, agents) — the
"what actually ran" counterpart to a playbook's static skill graph. A
node's ``attachments`` are the skills with at least one non-listing
observation (body/stub/execution) in that agent; ``ambient`` rolls up
the listings every agent silently re-injects. Edge ``to_agent_id`` is
null for orphan dispatches (no matching subagent transcript); orphan
edges are kept in ``edges`` so the renderer can show the gap, while
``totals["edges"]`` counts joined edges only. ``loose`` lists subagents
with no incoming dispatch edge — only when ``main`` exists to anchor
the tree.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .schema import AuditDocument, LoadEvidence, Observation, SkillEntity

ATTACHMENT_KINDS = ("body", "stub", "execution")

_HASH_RE = re.compile(r"([0-9a-f]{8,})$")


def _label(agent_id: str, subagent_types: Dict[str, Optional[str]]) -> str:
    subagent_type = subagent_types.get(agent_id)
    if subagent_type is None:
        return agent_id
    match = _HASH_RE.search(agent_id)
    if match is None:
        return subagent_type
    return "%s · %s" % (subagent_type, match.group(1)[:8])


def _attachment(skill: SkillEntity, observations: List[Observation],
                rows: List[LoadEvidence]) -> dict:
    body_sha1s = sorted({
        observation.body_sha1 for observation in observations
        if observation.body_sha1 is not None
    })
    channels = sorted({
        row.channel for row in rows if row.channel is not None
    })
    return {
        "skill_id": skill.skill_id,
        "name": skill.name,
        "aliases": list(skill.aliases),
        "executed": any(
            observation.kind == "execution" for observation in observations),
        "loads": [
            {
                "load_id": row.load_id,
                "kind": row.kind,
                "channel": row.channel,
                "ts": row.ts,
                "body_sha1": row.body_sha1,
                "cost_tokens_est": row.cost_tokens_est,
                "cost_basis": row.cost_basis,
            }
            for row in rows
        ],
        "loads_tokens_est": sum(
            row.cost_tokens_est or 0 for row in rows
            if row.cost_basis == "body"),
        "channels": channels,
        "sha1s": body_sha1s,
    }


def skill_tree(document: AuditDocument) -> dict:
    loads_by_agent: Dict[str, List[LoadEvidence]] = {}
    for load in document.skill_loads:
        loads_by_agent.setdefault(load.agent_id, []).append(load)

    subagent_types: Dict[str, Optional[str]] = {}
    edges: List[dict] = []
    for dispatch in document.dispatches:
        if dispatch.subagent_agent_id is not None:
            subagent_types.setdefault(
                dispatch.subagent_agent_id, dispatch.subagent_type)
        edges.append({
            "dispatch_id": dispatch.dispatch_id,
            "from_agent_id": dispatch.parent_agent_id,
            "to_agent_id": dispatch.subagent_agent_id,
            "subagent_type": dispatch.subagent_type,
            "description": dispatch.description,
            "brief_tokens_est": dispatch.brief_tokens_est,
            "phase_id": dispatch.phase_id,
        })

    parent_of: Dict[str, str] = {}
    via_dispatch: Dict[str, str] = {}
    for dispatch in document.dispatches:
        target = dispatch.subagent_agent_id
        if target is None or target in parent_of:
            continue
        parent_of[target] = dispatch.parent_agent_id
        via_dispatch[target] = dispatch.dispatch_id

    skills_by_id = {skill.skill_id: skill for skill in document.skills}
    nodes = []
    for agent in document.agents:
        attached: Dict[str, List[Observation]] = {}
        listing: Dict[str, List[int]] = {}
        for skill in document.skills:
            for observation in skill.observations:
                if observation.agent_id != agent.agent_id:
                    continue
                if observation.kind == "listing":
                    listing.setdefault(skill.skill_id, []).append(
                        observation.tokens_est)
                elif observation.kind in ATTACHMENT_KINDS:
                    attached.setdefault(skill.skill_id, []).append(observation)
        attachments = []
        for skill_id in sorted(attached):
            observations = attached[skill_id]
            rows = [load for load in loads_by_agent.get(agent.agent_id, [])
                    if load.skill_id == skill_id]
            attachments.append(
                _attachment(skills_by_id[skill_id], observations, rows))
        nodes.append({
            "agent_id": agent.agent_id,
            "label": _label(agent.agent_id, subagent_types),
            "model": agent.model,
            "context_window": agent.context_window,
            "n_requests": len(agent.request_ids),
            "parent_agent_id": parent_of.get(agent.agent_id),
            "via_dispatch_id": via_dispatch.get(agent.agent_id),
            "attachments": attachments,
            "ambient": {
                "skills": len(listing),
                "injections": sum(len(tokens) for tokens in listing.values()),
                "tokens_est": sum(
                    sum(tokens) for tokens in listing.values()),
            },
        })

    agent_ids = {agent.agent_id for agent in document.agents}
    loose = []
    if "main" in agent_ids:
        for agent in document.agents:
            if agent.agent_id == "main" or agent.agent_id in parent_of:
                continue
            loose.append({
                "agent_id": agent.agent_id,
                "label": _label(agent.agent_id, subagent_types),
                "reason": "no incoming dispatch edge",
            })

    totals = {
        "agents": len(document.agents),
        "edges": sum(1 for edge in edges if edge["to_agent_id"] is not None),
        "loads": sum(1 for load in document.skill_loads if load.kind == "load"),
        "reloads": sum(
            1 for load in document.skill_loads if load.kind == "reload"),
        "unavailable": sum(1 for load in document.skill_loads
                           if load.cost_basis == "unavailable"),
        "executions": sum(
            1 for skill in document.skills
            for observation in skill.observations
            if observation.kind == "execution"),
        "ambient_tokens_est": sum(
            node["ambient"]["tokens_est"] for node in nodes),
        "attached_tokens_est": sum(
            load.cost_tokens_est or 0
            for load in document.skill_loads if load.cost_basis == "body"),
    }
    return {"nodes": nodes, "edges": edges, "loose": loose, "totals": totals}
```

- [ ] **Step 4: Run the analysis tests**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_skill_tree.py -q`

Expected: `8 passed`.

- [ ] **Step 5: Add the 6th analyses key**

`src/agent_session_detective/ir/analyses.py` — after `from .schema import AuditDocument` (line 32):

```python
from .skill_tree import skill_tree
```

Add a docstring bullet after the ``dispatches`` bullet (lines 17-18):

```
- ``skill_tree``: the actual skill tree — agents as nodes, dispatch rows
  as edges, per-agent attachments and ambient listing cost.
```

and the key in `build_analyses` (lines 303-311):

```python
    return {
        "skill_audit": skill_audit(document),
        "context_organization": context_organization(document),
        "redundancy": redundancy(document),
        "skill_loads": skill_loads(document),
        "dispatches": dispatches(document),
        "skill_tree": skill_tree(document),
    }
```

- [ ] **Step 6: Update the three sorted-list sites**

`tests/test_cli.py` lines 178-182 and 198-202 — both sites are identical:

```python
            self.assertEqual(
                sorted(analyses),
                ["context_organization", "dispatches", "redundancy",
                 "skill_audit", "skill_loads", "skill_tree"],
            )
```

`tests/test_ir_analyses.py` lines 246-250:

```python
                self.assertEqual(
                    sorted(analyses),
                    ["context_organization", "dispatches", "redundancy",
                     "skill_audit", "skill_loads", "skill_tree"],
                )
```

- [ ] **Step 7: Run the full suite**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`

Expected: `232 passed`.

- [ ] **Step 8: Commit**

```bash
git add src/agent_session_detective/ir/skill_tree.py tests/test_skill_tree.py src/agent_session_detective/ir/analyses.py tests/test_cli.py tests/test_ir_analyses.py
git commit -m "feat: build the actual skill tree analysis (IR 1.2)"
```

---

### Task 4: Renderer — `tree_html.py`

The page never computes new facts: every number comes from `skill_tree(document)`
or the load ledger. Costs that are unknown render as the literal word
`unavailable` — a stub-derived number is never shown (design R9). Same
self-contained rules as `report.py`: no JavaScript, no external assets.

**Files:**
- Create: `src/agent_session_detective/tree_html.py`
- Create: `tests/test_tree_html.py`
- Reference (read-only): `src/agent_session_detective/report.py` lines 23-57 — the `CSS` constant is copied verbatim from here. That file is uncommitted; do not modify it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tree_html.py` with exactly:

```python
"""Renderer contract tests for the standalone skill-tree page."""

import unittest

from agent_session_detective.tree_html import render_skill_tree
from tests.test_skill_tree import (CHILD, MAIN, agent, dispatch, document,
                                   load, observation, skill)


def page(*args, **kwargs):
    return render_skill_tree(document(*args, **kwargs))


class SelfContainedTest(unittest.TestCase):
    def test_no_script_and_no_external_urls(self):
        out = page(agents=[agent(MAIN)],
                   skills=[skill("demo", [observation("body", tokens=40)])],
                   loads=[load("main:load:1", MAIN, "demo", cost=40)])
        self.assertNotIn("<script", out)
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)

    def test_determinism(self):
        kwargs = dict(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("body", tokens=40)])],
            loads=[load("main:load:1", MAIN, "demo", cost=40)])
        self.assertEqual(page(**kwargs), page(**kwargs))


class UnavailableTest(unittest.TestCase):
    def test_unavailable_renders_the_word_never_a_number(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("stub", ts=1.0)])],
            loads=[load("main:load:1", MAIN, "demo", channel=None,
                        cost=None, basis="unavailable")])
        self.assertIn("unavailable", out)
        self.assertNotIn("~", out)

    def test_a_known_cost_renders_as_est(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("demo", [observation("body", tokens=37)])],
            loads=[load("main:load:1", MAIN, "demo", cost=37)])
        self.assertIn("~37 EST", out)


class EscapingTest(unittest.TestCase):
    def test_escaping(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill('a<b&"c', [observation("body", tokens=10)])],
            loads=[load("main:load:1", MAIN, 'a<b&"c', cost=10)],
            dispatches=[dispatch("main:dispatch:1", MAIN, CHILD, "<i>t</i>",
                                 description="<b>bold</b>", brief=None)])
        self.assertIn("a&lt;b&amp;&quot;c", out)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", out)


class AliasChipTest(unittest.TestCase):
    def test_alias_chip_is_rendered_from_the_entity(self):
        out = page(
            agents=[agent(MAIN)],
            skills=[skill("ns:demo", [observation("body", tokens=37)],
                          name="demo", aliases=["demo"])],
            loads=[load("main:load:1", MAIN, "ns:demo", cost=37)])
        self.assertIn("(alias: demo)", out)
        self.assertIn("ns:demo", out)


class StructureTest(unittest.TestCase):
    def test_cycle_guard_renders_the_note_and_terminates(self):
        out = page(
            agents=[agent(MAIN), agent("A", request_ids=("a:0",))],
            dispatches=[dispatch("a:dispatch:1", "A", MAIN, "subagent"),
                        dispatch("main:dispatch:1", MAIN, "A", "subagent")])
        self.assertIn("already on this path", out)

    def test_orphan_and_loose_are_rendered(self):
        out = page(
            agents=[agent(MAIN), agent(CHILD)],
            dispatches=[dispatch("main:dispatch:1", MAIN, None, "subagent")])
        self.assertIn("orphan dispatch", out)
        self.assertIn("no incoming dispatch edge", out)

    def test_honest_empty_node(self):
        out = page(agents=[agent(MAIN)])
        self.assertIn("no skill loads, no executions", out)
        self.assertIn("(counted without rows)", out)


class EmptyDocumentTest(unittest.TestCase):
    def test_empty_document(self):
        out = page(agents=[])
        self.assertIn("no agents in document", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test file to verify it fails**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_tree_html.py -q`

Expected: collection error — `ModuleNotFoundError: No module named 'agent_session_detective.tree_html'`.

- [ ] **Step 3: Write the implementation**

Create `src/agent_session_detective/tree_html.py` with exactly:

```python
"""Render the actual skill tree as a self-contained HTML page.

No JavaScript and no external assets, same rules as report.py. Every
number is either counted from the document or the literal word
``unavailable`` — a stub-derived cost is never shown (design R9).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Dict, List, Optional

from .ir.schema import AuditDocument
from .ir.skill_tree import skill_tree

CSS = """
:root { --ink:#1a1d21; --dim:#5b6470; --line:#e3e6ea; --fact:#0b6bcb;
        --warn:#b25e09; --bad:#c0392b; --ok:#1e7d46; --bg:#f7f8fa;
        --inject:#7048e8; --skill:#c2255c; }
* { box-sizing:border-box; }
body { font:15px/1.6 -apple-system,"SF Pro","PingFang SC",sans-serif;
       color:var(--ink); margin:0; background:#fff; }
header { background:var(--bg); border-bottom:1px solid var(--line); padding:28px 36px; }
h1 { margin:0 0 6px; font-size:22px; }
h2 { font-size:17px; margin:36px 0 12px; padding-top:20px; border-top:1px solid var(--line); }
.meta { color:var(--dim); font-size:13px; }
main { max-width:960px; margin:0 auto; padding:8px 36px 64px; }
.badge { display:inline-block; font-size:12px; padding:1px 8px; border-radius:10px;
         background:var(--bg); border:1px solid var(--line); color:var(--dim); margin-right:6px; }
.badge.fact { color:var(--fact); border-color:var(--fact); }
.badge.infer { color:var(--warn); border-color:var(--warn); }
.badge.missed { color:var(--bad); border-color:var(--bad); }
.badge.ok { color:var(--ok); border-color:var(--ok); }
details { border:1px solid var(--line); border-radius:8px; margin:8px 0; }
summary { cursor:pointer; padding:10px 14px; font-weight:600; }
details[open] summary { border-bottom:1px solid var(--line); }
.body { padding:12px 14px; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:6px;
      padding:10px 12px; overflow-x:auto; font-size:12.5px; line-height:1.5; white-space:pre-wrap; }
blockquote { margin:8px 0; padding:6px 12px; border-left:3px solid var(--fact);
             background:var(--bg); }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
td,th { border:1px solid var(--line); padding:5px 10px; text-align:left; vertical-align:top; }
th { background:var(--bg); }
.notice { padding:10px 14px; border-radius:8px; background:#fdf3e7;
          border:1px solid var(--warn); color:var(--warn); }
.timeline-item { display:flex; gap:12px; padding:8px 0; border-bottom:1px dashed var(--line); }
.timeline-ts { color:var(--dim); font-size:12.5px; white-space:nowrap; width:150px; }
.empty { color:var(--dim); font-style:italic; }
"""

MAX_DEPTH = 12


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _cost(value: Optional[int]) -> str:
    if value is None:
        return "<span class='badge missed'>unavailable</span>"
    return "~%d EST" % value


def _footer(document: AuditDocument) -> str:
    loads = document.skill_loads
    n_loads = sum(1 for load in loads if load.kind == "load")
    n_reloads = sum(1 for load in loads if load.kind == "reload")
    n_unavailable = sum(
        1 for load in loads if load.cost_basis == "unavailable")
    evidence = document.coverage.skill_load_evidence or {}
    links = document.coverage.dispatch_links or {}
    recognition = document.coverage.phase_recognition or {}
    identity = document.coverage.skill_identity or {}
    text = ("%d loads / %d reloads / %d unavailable / %d redundant bodies "
            "(counted without rows)") % (
                n_loads, n_reloads, n_unavailable,
                evidence.get("redundant_bodies", 0))
    text += (" · dispatches %d / joined via records %d / joined via meta.json "
             "only %d / orphan dispatches %d") % (
                 links.get("dispatches", 0), links.get("joined", 0),
                 links.get("joined_via_meta_only", 0),
                 links.get("orphan_dispatches", 0))
    text += " · phase tiers A/B/C %d/%d/%d" % (
        recognition.get("tierA", 0), recognition.get("tierB", 0),
        recognition.get("tierC", 0))
    if not recognition.get("rule_set_version"):
        text += " / phase rules uncalibrated (rule set empty)"
    if identity.get("merges"):
        pairs = ", ".join(
            "%s ← %s" % (key, ", ".join(identity["aliases"][key]))
            for key in sorted(identity.get("aliases", {})))
        text += " · identity merges %d (%s)" % (identity["merges"], pairs)
    if identity.get("ambiguous"):
        text += " / ambiguous ids stay unmerged: %s" % ", ".join(
            identity["ambiguous"])
    return "<div class='meta'>%s</div>" % esc(text)


def _render_node(node: dict, nodes_by_id: Dict[str, dict],
                 children: Dict[str, List[dict]], orphans: List[dict],
                 path: set, depth: int) -> str:
    if depth >= MAX_DEPTH:
        return ("<div class='notice'>depth limit reached at %s; not expanded"
                "</div>" % esc(node["label"]))
    path = path | {node["agent_id"]}
    ambient = node["ambient"]
    summary = ("%s <span class='badge'>%s</span> "
               "<span class='badge'>%d requests</span>" % (
                   esc(node["label"]), esc(node["model"] or "model ?"),
                   node["n_requests"]))
    if ambient["skills"] > 0:
        summary += (" <span class='badge'>preloaded: %d skills, %d injections, "
                    "~%d EST</span>" % (
                        ambient["skills"], ambient["injections"],
                        ambient["tokens_est"]))
    body = []
    if not node["attachments"] and ambient["skills"] == 0:
        body.append("<p class='empty'>no skill loads, no executions</p>")
    for attachment in node["attachments"]:
        head = esc(attachment["name"])
        if attachment["name"] != attachment["skill_id"]:
            head += " <span class='badge'>%s</span>" % esc(
                attachment["skill_id"])
        for alias in attachment["aliases"]:
            head += " <span class='badge miss'>(alias: %s)</span>" % esc(alias)
        if attachment["executed"]:
            head += " <span class='badge ok'>executed</span>"
        rows = attachment["loads"]
        if rows:
            if attachment["loads_tokens_est"] > 0:
                total = _cost(attachment["loads_tokens_est"])
            else:
                total = "<span class='badge missed'>unavailable</span>"
        else:
            total = "—"
        rows_html = []
        for row in rows:
            kind_class = "ok" if row["kind"] == "load" else "infer"
            rows_html.append(
                "<tr><td><span class='badge %s'>%s</span></td><td>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    kind_class, esc(row["kind"]),
                    esc(row["channel"]) if row["channel"] else "—",
                    esc(fmt_ts(row["ts"])),
                    esc(row["body_sha1"][:8]) if row["body_sha1"] else "—",
                    _cost(row["cost_tokens_est"]),
                ))
        body.append(
            "<details open><summary>%s</summary><div class='body'>"
            "<div class='meta'>total %s</div>%s</details>" % (
                head, total,
                ("<table><tr><th>kind</th><th>channel</th><th>ts</th>"
                 "<th>sha1</th><th>cost</th></tr>%s</table>"
                 % "".join(rows_html)) if rows_html
                else "<p class='empty'>no load rows</p>"))
    for edge in children.get(node["agent_id"], []):
        body.append(
            "<p class='meta'>dispatch · brief %s · %s · %s</p>" % (
                _cost(edge["brief_tokens_est"]),
                esc(edge["subagent_type"] or "?"),
                esc(edge["description"] or "")))
        child = nodes_by_id.get(edge["to_agent_id"])
        if child is None:
            body.append(
                "<p class='empty'>dispatch target %s has no transcript</p>"
                % esc(edge["to_agent_id"]))
            continue
        if child["agent_id"] in path:
            body.append(
                "<p class='notice'>cycle: %s is already on this path; "
                "not expanded</p>" % esc(child["label"]))
            continue
        body.append(_render_node(child, nodes_by_id, children, orphans,
                                 path, depth + 1))
    for edge in orphans:
        if edge["from_agent_id"] == node["agent_id"]:
            body.append(
                "<p class='notice'>orphan dispatch: no matching subagent "
                "transcript (dispatch %s)</p>" % esc(edge["dispatch_id"]))
    return ("<details open><summary>%s</summary><div class='body'>%s</div>"
            "</details>" % (summary, "".join(body)))


def render_skill_tree(document: AuditDocument) -> str:
    tree = skill_tree(document)
    nodes_by_id = {node["agent_id"]: node for node in tree["nodes"]}
    children: Dict[str, List[dict]] = {}
    orphans: List[dict] = []
    for edge in tree["edges"]:
        if edge["to_agent_id"] is None:
            orphans.append(edge)
        else:
            children.setdefault(edge["from_agent_id"], []).append(edge)

    totals = tree["totals"]
    parts = [
        "<!DOCTYPE html>",
        "<html lang='zh'>",
        "<head><meta charset='utf-8'>",
        "<title>Actual Skill Tree</title>",
        "<style>%s</style>" % CSS,
        "</head>",
        "<body>",
        "<header>",
        "<h1>Actual Skill Tree</h1>",
        "<div class='meta'>%s</div>" % " · ".join(
            esc(source) for source in document.source_files),
        "<div class='meta'>"
        "<span class='badge'>IR %s</span>"
        "<span class='badge'>%d agents</span>"
        "<span class='badge'>%d edges</span>"
        "</div>" % (esc(document.ir_version), totals["agents"],
                    totals["edges"]),
        "<div class='meta'>costs are EST (estimated from observed body "
        "tokens); unavailable means no body was observed — a stub-derived "
        "number is never shown.</div>",
        "</header>",
        "<main>",
    ]
    if not tree["nodes"]:
        parts.append("<p class='empty'>no agents in document</p>")
    else:
        if "main" in nodes_by_id:
            roots = [nodes_by_id["main"]]
        else:
            roots = [node for node in tree["nodes"]
                     if node["parent_agent_id"] is None]
        for root in roots:
            parts.append(_render_node(root, nodes_by_id, children, orphans,
                                      set(), 0))
    if tree["loose"]:
        parts.append("<h2>agents without a dispatch edge</h2>")
        for entry in tree["loose"]:
            parts.append("<p>%s <span class='badge'>%s</span></p>" % (
                esc(entry["label"]), esc(entry["reason"])))
    parts.append(_footer(document))
    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)
```

Notes on decisions baked into this code:

- The ambient badge renders only when `ambient["skills"] > 0`; a zero-ambient node must not emit a `~0 EST` string (the unavailable test asserts the tilde never appears).
- The orphan notice names the `dispatch_id`; the edge dict carries exactly the seven keys Task 3 locked.
- The `CSS` block above is a verbatim copy of `report.py` lines 23-57. If it differs by a character from that file, copy the file's version — it is the reference.

- [ ] **Step 4: Run the test file to verify it passes**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_tree_html.py -q`

Expected: `10 passed`.

- [ ] **Step 5: Run the full suite**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`

Expected: `242 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/tree_html.py tests/test_tree_html.py
git commit -m "feat: render the actual skill tree as a standalone page (IR 1.2)"
```

### Task 5: CLI `--tree-out` (D4)

**Files:**
- Modify: `src/agent_session_detective/cli.py` (import block after line 18; argparse after the `--ir-analyses` block, lines 56-57; main body after the `--ir-analyses` block, lines 117-120)
- Create: `tests/test_cli_tree.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_tree.py`:

```python
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
```

This mirrors `tests/test_cli.py`'s `IrCliTests.run_cli` pattern: the same `tier1.jsonl` copy into a tempdir, the same `render_report` patch, and an in-process `cli.main` call.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_cli_tree.py -q`

Expected: FAIL — pytest reports `SystemExit: 2`: argparse exits because `--tree-out` is not a recognised option yet.

- [ ] **Step 3: Wire the CLI**

Three edits to `src/agent_session_detective/cli.py`:

(a) Import — after `from .tokenstats import build_token_stats` (line 18), keeping imports alphabetical:

```python
from .tokenstats import build_token_stats
from .tree_html import render_skill_tree
from .wire import (
```

(b) Argument — insert between the `--ir-analyses` option (lines 56-57) and `--open` (line 58):

```python
    parser.add_argument("--ir-analyses", default=None, metavar="PATH",
                        help="Write the three IR analyses as JSON (builds the IR in memory).")
    parser.add_argument("--tree-out", default=None, metavar="PATH",
                        help="Render the actual skill tree (agents → dispatches → "
                             "skill loads) as a standalone HTML page.")
    parser.add_argument("--open", action="store_true", help="Open the report in a browser.")
```

(c) Main body — extend the `--ir-analyses` block (lines 119-120). The `document` built at line 116 feeds both outputs:

```python
    if args.ir_analyses:
        _write_json(Path(args.ir_analyses), build_analyses(document))
    if args.tree_out:
        tree_path = Path(args.tree_out)
        tree_path.write_text(render_skill_tree(document), encoding="utf-8")
        print(tree_path)
```

- [ ] **Step 4: Run the test file**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest tests/test_cli_tree.py -q`

Expected: `1 passed`.

- [ ] **Step 5: Run the full suite**

Run: `cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m pytest -q`

Expected: `243 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/cli.py tests/test_cli_tree.py
git commit -m "feat: add --tree-out to the CLI (IR 1.2)"
```

### Task 6: Demo acceptance on a real session (local-only)

**No code changes, no commits.** Everything below writes to `/tmp`; the session file is only read. The session and any text derived from it stay local — do not commit the demo outputs.

The demo session `f1c47018` is the Qoder transcript used to calibrate the spec: 3 agents (main with 25 requests, two subagents), 2 dispatches, 3 skill loads, 1 identity merge.

- [ ] **Step 1: Generate the page for the demo session**

Run from the repo root:

```bash
cd /Users/wangting/work/agent-session-detective && PYTHONPATH=src python3 -m agent_session_detective.cli \
  "$HOME/.qoder/projects/-Users-wangting-work-agent-session-detective/f1c47018-7991-4012-9bbb-d2e8f8fbfcb7.jsonl" \
  --no-judge --out /tmp/asd-tree-demo-report.html --tree-out /tmp/asd-tree-demo.html
```

Expected: exit code 0; stdout prints both `/tmp/asd-tree-demo-report.html` and `/tmp/asd-tree-demo.html`.

- [ ] **Step 2: Verify the page content**

```bash
for needle in \
  "Actual Skill Tree" \
  "IR 1.2" \
  "3 agents" \
  "2 edges" \
  "25 requests" \
  "preloaded: 109 skills, 249 injections, ~20284 EST" \
  "pstack:poteto-mode" \
  "(alias: poteto-mode)" \
  "pstack:poteto-agent · 241caed7" \
  "~889 EST" \
  "~1000 EST" \
  "~5252 EST" \
  "~538 EST" \
  "~357 EST" \
  "3 loads / 0 reloads / 0 unavailable / 0 redundant bodies" \
  "joined via meta.json only 2" \
  "phase tiers A/B/C 0/0/2" \
  "phase rules uncalibrated (rule set empty)" \
  "identity merges 1 (pstack:poteto-mode ← poteto-mode)"; do
  printf '%-72s %s\n' "$needle" "$(grep -cF "$needle" /tmp/asd-tree-demo.html)"
done
```

Expected: every line prints a count ≥ 1.

- [ ] **Step 3: Verify the honest-empty child and self-containment**

```bash
grep -cF "no skill loads, no executions" /tmp/asd-tree-demo.html
grep -cF "<script" /tmp/asd-tree-demo.html
```

Expected: `1` (the second subagent node is honest-empty) and `0` (the page stays script-free).

- [ ] **Step 4: Confirm the workspace is clean**

```bash
cd /Users/wangting/work/agent-session-detective && git status --short
```

Expected: only the plan file `docs/superpowers/plans/2026-10-09-actual-skill-tree.md` is modified — no demo output, no code drift from Tasks 1-5 left uncommitted.
