# Actual Skill Tree: Design

## Goal

Render the **actually-executed skill tree** of a session from its
`AuditDocument` — which skills each agent really loaded, which agent
dispatched which, in what order, at what cost. The input is the existing
IR 1.1 evidence (load ledger, dispatches, executions); the output is a
single-file, no-JavaScript HTML page, deterministic from the document
alone.

The motivating comparison is pstack's two static playbook graphs
(`pstack-bugfix-skill-graph.html`, `pstack-feature-skill-graph.html`),
which were hand-derived from playbook markdown — they describe what the
playbook *declares*, not what a session *did*. This design renders the
latter, and only the latter: every node, edge, and cost in the tree
traces back to a ledger row, a dispatch row, or a skill observation.

The founding disciplines carry over unchanged: coverage is re-derived,
never self-declared; telemetry the source does not record is labeled
unavailable, never fabricated.

## Scope

In scope (IR 1.2):

- `ir/skills.py`: skill identity canonicalization — merge the bare-id
  projection (`poteto-mode`, from file-path-derived Read attribution)
  into its namespaced twin (`pstack:poteto-mode`, from the Skill tool
  call), under explicit guards; alternatives recorded as aliases.
- `ir/skill_tree.py`: the tree analysis — nodes (agents), edges
  (dispatches), per-node attachments (loaded/executed skills) and
  ambient context (listing observations), read only from the document.
- `tree_html.py`: a new renderer module producing one self-contained
  HTML page per document — no JavaScript, no external assets, same
  visual conventions as `report.py`.
- `cli.py`: a `--tree-out PATH` flag writing the tree page; the document
  is already always built (`cli.py:112`).
- Schema additions: `SkillEntity.aliases`, `Observation.raw_skill_id`,
  `CoverageReport.skill_identity`; `IR_VERSION` 1.1 → 1.2.
- Tests per the Tests section, including the identity-merge guard cases
  (own-listing counter-example, ambiguity) and renderer honesty rules.

Out of scope (1.2):

- **No phase zones in the tree.** Phase attribution is gated on
  calibration ③ (`phase_rules.py` ships empty); until then the tree
  shows dispatches as edges with briefly-labeled children, not phase
  columns. When calibration lands, zones are a renderer addition over
  the same analysis.
- **No playbook overlay** (approach B: aligning the static playbook
  graph against the actual tree) — a follow-up, not part of this
  rendering.
- No webapp integration; the tree is its own artifact, not a section of
  `report.py`'s skill-audit page.
- No SVG/force layout engine; nesting is rendered as indented HTML.
- No new parsing — the analysis reads `AuditDocument` only.
- Kimi/Codex trees: the analysis is adapter-agnostic (it reads document
  fields already populated per adapter), but only Qoder produces
  dispatch rows today, so other sources render single-node trees with
  whatever loads exist.

## Corpus reality

The design is grounded on one real session already on disk (the
`f1c47018` Qoder session in this project's own transcript directory),
used as the demo corpus throughout:

- 3 agents: `main` (25 requests), and two subagent transcripts
  (`subagent:agent-apstack:poteto-agent-241caed765ad6b60`, 150
  requests; `subagent:…b3e853343b5f35a6`, 111 requests).
- 2 dispatches, both `subagent_type: pstack:poteto-agent`, briefs 889
  and 1000 EST; both joined via meta.json only (`joined_via_meta_only:
  2`) — the demo exercises the fallback join path.
- 3 loads, all in the first subagent, all channel `tool:read`:
  `poteto-mode` 5252 EST (today's bare id; canonicalized to
  `pstack:poteto-mode` by D1's merge — Finding 1 shows they are one
  skill), `principle-model-the-domain` 538 EST,
  `principle-prove-it-works` 357 EST.
- 1 execution: `pstack:poteto-mode`, on `main`.
- Ambient listings: 109 entities, 249 injections, 20,284 EST, all on
  `main`.
- Phase recognition: tier C both dispatches (`rule_set_version: null`).
- The second subagent has **no attachments at all** — the honest-empty
  node, deliberately kept in acceptance.

No bugfix/feature pstack sessions exist on disk yet; the user supplies
one later for the follow-up (approach B / phase zones). Never baseline
from the retired 2026-07-09 sessions
(`0091bc97-318e-44a4-84f8-477c31fbfbee`,
`370896c5-9210-4b9e-a501-8f197a1171af`).

## Verified findings

### Finding 1 — skill identity splits in two at the parser, silently

One real skill shows up as two `SkillEntity`s:

- `pstack:poteto-mode` (namespaced) — from the Skill tool call argument
  (`{"skill": "pstack:poteto-mode"}` → `target.lower()`,
  `items.py:171`). Carries the execution observation.
- `poteto-mode` (bare) — from the subagent's `Read` of
  `…/cursor-plugins/pstack/skills/poteto-mode/SKILL.md`; the folder
  basename projection (`os.path.basename(os.path.dirname(file_path))`,
  `items.py:223`) drops the plugin namespace. Carries the load
  observation (5252 EST, channel `tool:read`).

Verified against the raw transcript: the read path is under
`cursor-plugins/pstack/skills/poteto-mode/SKILL.md` — same skill,
two ids. Consequence today: the tree would show an execution on `main`
and a load in the subagent as **unrelated skills**; plan and principle
files (`principle-model-the-domain`, `principle-prove-it-works`) load
with bare ids too.

### Finding 2 — the bare↔namespaced merge needs a guard, and the corpus has a live counter-example

Bare and namespaced variants also exist for skills that are genuinely
different: in the same session, bare `pptx` has its own `skill_listing`
line on `main` while `presentations:pptx` has three listings. A
blanket bare→namespaced merge would be wrong. The merge rule below
encodes this: merge only when the bare id has **no listing line of its
own** and exactly one namespaced candidate exists.

### Finding 3 — tree edges must come from dispatch rows, not `parent_id`

`records.py:187` hardcodes every Qoder subagent's `parent_id` to
`"main"`, and `records.py:184` does the same for sidechains — a static
attribution placeholder, not a link. The authoritative structure is the
dispatch join (`Dispatch.parent_agent_id` → `subagent_agent_id`, IR
1.1), which by definition supports nesting. The tree analysis reads
dispatch rows for edges and never `parent_id`.

## D1 — Skill identity canonicalization (`ir/skills.py`)

New function `merge_skill_identities(entities, listing_skill_ids) ->
(List[SkillEntity], dict)`, run inside `builder.py` between
`build_skills(...)` (line 308) and `build_loads(...)` (line 311), so
every downstream consumer — the load ledger's `_collect_entries`
(`skill_id=skill.skill_id`), the load-evidence coverage block, the
tree analysis — sees canonical ids. Pairing state in `loads.py`
(`(agent_id, skill_id)` keys) stays intact because merging happens
before it runs.

**Merge rule.** A bare id `X` merges into a namespaced id
`plugin:X` when **both**:

1. exactly one existing entity's id ends with `":X"` — the candidate;
2. `X` has no `skill_listing` observation of its own in that session.

Merged entity: the namespaced `skill_id`; the bare entity's
observations are appended (each stamped `raw_skill_id = "X"` for
provenance) and re-sorted by the existing `(ts is None, ts, item_id,
kind)` key; `aliases` gains the bare id. Never merge two namespaced ids
(`a:X` and `b:X` both existing → rule 1 yields ≥2 candidates →
**ambiguous: no merge**, counted in coverage).

**Name resolution** (display only): keep the namespaced entity's
existing name when it differs from its own id; otherwise take the
merged-in entity's name when that differs from its id; otherwise the
bare id. For `pstack:poteto-mode` the chain yields the bare name
`poteto-mode` — the friendly label the static graphs use.

**What never changes:** `skill_id` strings of unmerged entities,
observation ordering semantics, `loads.py` walk rules, cost fields.
Merging is pure id canonicalization — no observation is invented,
dropped, or re-timed.

`CoverageReport.skill_identity` (new block, re-derived):
`{merges: n, aliases: {merged_id: [bare_ids]}, ambiguous: [bare_ids],
ir_version note}` — ambiguous and unmerged cases stay visible instead
of silently normalized.

**Consumer consequence, stated:** `SkillEntity` counts in
`skill_audit` drop by the number of merges; load rows that previously
carried a bare `skill_id` now carry the namespaced id (for f1c47018:
the 5252-EST tool:read row moves from `poteto-mode` to
`pstack:poteto-mode`). The judge/expect path (`timeline.py`) is
untouched — it never consumed IR entities.

## D2 — The tree analysis (`ir/skill_tree.py`)

New module; `build_analyses(document)` gains a 6th key `"skill_tree"`.
`skill_tree(document)` returns:

```
{
  "nodes": [
    { "agent_id", "label", "model", "context_window", "n_requests",
      "parent_agent_id", "via_dispatch_id",
      "attachments": [ {skill_id, name, aliases, executed, loads: [...],
                        loads_tokens_est, channels: [...], sha1s: [...]} ],
      "ambient": { "skills": n, "injections": n, "tokens_est": n } }
  ],
  "edges": [
    { "dispatch_id", "from_agent_id", "to_agent_id", "subagent_type",
      "description", "brief_tokens_est", "phase_id" } ],
  "loose": [ {agent_id, label, reason} ],
  "totals": { "agents", "edges", "loads", "reloads", "unavailable",
              "executions", "ambient_tokens_est", "attached_tokens_est" }
}
```

**Nodes** — one per `document.agents` entry. `label` is
`subagent_type · hash8` for subagent nodes when the dispatch provides a
`subagent_type` (e.g. `pstack:poteto-agent · 241caed7`), else the agent
id; `hash8` is the first 8 hex chars of the transcript-derived hash in
the agent id. Parent link: the edge whose `to_agent_id` matches;
`via_dispatch_id` records it. A node with no incoming edge and not
`main` goes to `loose` (counted, then rendered in full in a closing
"agents without a dispatch edge" section — loose agents are unreachable
from `main`'s subtree, so rendering them there cannot double-render),
never dropped.

**Attachments** — for each node, skills with ≥1 non-listing observation
**in that agent** (any of: `body`, `stub`, `execution`). Per skill:
executed flag (`execution` observation present in that agent), the
node's `LoadEvidence` rows for that skill (from `document.skill_loads`,
`load.agent_id == node.agent_id`), summed `tokens_est` over body-basis
rows (`cost_basis == "body"`), channel set, body sha1 list. A skill that appears in the node
only through `skill_listing` observations is **not** an attachment —
listings are ambient, per skill never per-listing rows.

**Ambient** — per node: count of distinct skills with ≥1 `listing`
observation in that agent, total listing observation count, summed
listing `tokens_est`. No exclusion logic: this is the one-line "what
the harness preloaded into this agent" readout (f1c47018 main: 109 /
249 / 20284).

**Edges** — one per `document.dispatches` row, always. `from` =
`parent_agent_id`, `to` = `subagent_agent_id` when joined, else the
edge is emitted with `to_agent_id: null` and the renderer shows it as
an orphan note under the parent. The per-row join source (records vs
meta.json) is not stored on `Dispatch` — only the aggregate counts in
`dispatch_links` exist, and the coverage strip is where that note
lives; the edge row claims nothing beyond the join itself. Nested
dispatches work by construction (`parent_agent_id` may itself be a
subagent).

**Totals** — re-derived from the rows above; `attached_tokens_est`
sums `cost_basis == "body"` rows across the whole ledger (a hand-built
document whose rows name an agent absent from `document.agents` counts
in the total, not under any node), `unavailable` counts null-cost rows.
The analysis never reads transcript text, only the document.

## D3 — The tree renderer (`tree_html.py`)

`render_skill_tree(document) -> str`: one self-contained HTML string.
No JavaScript, no external URLs, no fonts — same discipline as
`report.py`. Reuses `report.py`'s conventions without importing its
internals: same CSS variable palette (`--ink/--dim/--line/--fact/
--warn/--bad/--ok/--bg/--skill/--inject`), `html.escape`-based `esc()`,
`fmt_ts()`, `<details>` expandables, ~960px main column. No dark mode
(none exists in `report.py`; consistency wins over novelty).

Layout, top to bottom:

1. **Header** — session title line, `IR_VERSION`, agent count, edge
   count; a one-line legend: chips `load` / `reload` / `executed`,
   costs are EST (`~`), and `unavailable` means no body was found —
   never a number.
2. **Tree** — `main` (or roots when no `main` node) rendered as nested
   `<details open>` blocks; connector lines via CSS borders on the
   indent column; each node header shows label, model, requests,
   context window. Under each node: **attachments** table (skill,
   executed?, loads with per-load kind/ts/channel/cost, `unavailable`
   for null-cost rows, summed EST), and an **ambient** line when
   `skills > 0` ("preloaded: N skills, M injections, ~T EST").
   Edges render as the child block itself, with the dispatch line
   above it: brief EST, subagent_type, description when present; orphan
   dispatches render as a `warn` note under the parent; `loose` agents
   render in a closing section with their reason followed by the same
   node block (attachments, dispatches, orphan notices).
3. **Coverage strip** — footer with honesty labels, all from coverage:
   loads/reloads/unavailable, redundant bodies, `joined_via_meta_only`
   note, phase tier counts with the explicit "phase rules uncalibrated
   (rule set empty)" line when `rule_set_version` is null, identity
   merges with aliases, snapshot note that costs are EST.

Honesty rules enforced by the renderer and tested:

- A null `cost_tokens_est` renders the literal word `unavailable` —
  never a number, never a stub-derived estimate.
- Costs render with `~` and an `EST` legend.
- Empty nodes render an explicit "no skill loads, no executions"
  line (the honest-empty case, exercised by the second f1c47018
  subagent).
- All text is HTML-escaped; brief `description` text comes from the
  document as logged (fact), rendered escaped.

## D4 — CLI (`cli.py`)

`--tree-out PATH` (metavar `PATH`): writes `render_skill_tree(document)`
to the path and prints it, exactly the `--ir-out` pattern
(`cli.py:113-114`). The document is already always built (line 112), so
no other CLI change. `--tree-out` is independent of `--ir-out` /
`--ir-analyses`; any combination works.

No webapp code changes: `web.py` imports the specific `skill_loads` view
(`web.py:552`), not `build_analyses`, so the 6th analyses key and the
new flag are additive. The result fingerprint must still bump v6 → v7:
cached judge results embed the `skill_loads` ledger, whose ids IR 1.2
canonicalizes — the same class of change as the v5 → v6 bump in
`1f1c1e4`.

## Schema delta and versioning

`IR_VERSION` 1.1 → 1.2. Additive:

- `SkillEntity.aliases: List[str] = []`;
- `Observation.raw_skill_id: Optional[str] = None`;
- `CoverageReport.skill_identity: dict = {}`.

No non-additive changes; no item-kind, bucket, or ordering changes.
Golden fixtures asserting `ir_version "1.1"` move to `"1.2"`.
`build_analyses` gains the `"skill_tree"` key — additive for CLI
consumers of `--ir-analyses`.

## Fact/est discipline additions

- **FACT**: the two id strings and the read path that produced the
  bare one; listing lines; dispatch rows and their joins; observation
  counts; sha1s.
- **DERIVED** (stated, checked rules): the bare↔namespaced merge under
  its two guards; node attachment membership (non-listing observation
  in that agent); ambient rollups; orphan/loose classification.
- **EST**: every `tokens_est` rendered, including per-load costs and
  ambient sums; all rendered with `~` and the EST legend.
- **Never fabricated**: a merged id where the guards fail (ambiguous
  stays unmerged and is counted); an attachment without a non-listing
  observation; an edge without a dispatch row; a load cost without a
  body (`unavailable`, no number); a phase attribution while the rule
  set is empty (`phase_id: null`, tier C).

## Error handling and degradation

- Bare id with ≥2 namespaced candidates: no merge, listed in
  `skill_identity.ambiguous`.
- Bare id with a listing of its own: no merge (the `pptx` case).
- Orphan dispatch (no subagent file): edge with `to_agent_id: null`,
  rendered as a warning note; counted.
- Unlinked agent (no incoming edge, not `main`): `loose`, rendered,
  counted.
- Cycle in dispatch rows (hand-built document): renderer caps nesting
  depth at a fixed limit and renders a cycle note instead of
  descending; analysis itself stores edges as flat rows.
- Empty document / no agents: renderer emits the header and an explicit
  "no agents in document" line — never a blank page.
- Non-Qoder adapters: no dispatches → single node; loads render if the
  adapter produced skill observations.

## Module placement

- `ir/skills.py` — `merge_skill_identities` (+ helper for the suffix /
  listing guard).
- `ir/skill_tree.py` — `skill_tree(document)`; pure document reads.
- `ir/analyses.py` — one added key in `build_analyses`.
- `ir/builder.py` — the merge call between `build_skills` and
  `build_loads`; `skill_identity` coverage block wiring.
- `ir/schema.py` — three fields; `IR_VERSION` bump.
- `ir/coverage.py` — `skill_identity` keyword block.
- `tree_html.py` — `render_skill_tree(document)` (top-level, beside
  `report.py`; it is a sibling artifact, not part of the audit report).
- `cli.py` — `--tree-out`.

## Tests

TDD, one red-green cycle per behavior:

- **Identity merge** (`tests/test_skill_identity.py`):
  - namespaced + bare merge; observations appended with
    `raw_skill_id`; ordering preserved; aliases recorded;
  - name resolution: namespaced name == own id → takes bare name;
    namespaced name differs → keeps its own;
  - own-listing guard: bare id with its own listing does not merge
    (the `pptx` shape);
  - ambiguity: two namespaced candidates → no merge, counted;
  - unrelated bare ids untouched;
  - `skill_identity` coverage block counts.
- **Tree analysis** (`tests/test_skill_tree.py`):
  - nodes/edges from a hand-built document; `main` root; subagent
    label with type + hash8;
  - attachments only for non-listing observations; listing-only skill
    is ambient, not an attachment;
  - per-node load rows joined by `load.agent_id`; null-cost row kept
    and counted unavailable;
  - orphan dispatch → edge with null `to`; unlinked agent → `loose`;
  - totals re-derived.
- **Renderer** (`tests/test_tree_html.py`):
  - no `<script` and no external URLs (`http(s)://`, `src=`,
    `href=` to foreign hosts) in output;
  - `unavailable` rendered for null-cost load, no digits claimed as
    its cost;
  - escaping: skill names / descriptions with `<`, `&`, quotes render
    escaped;
  - honest-empty node line;
  - cycle guard renders the note and terminates;
  - determinism: same document → byte-identical output.
- **CLI** (`tests/test_cli_tree.py`): `--tree-out` writes the file,
  prints the path (tmp_path); document built without `--ir-out`.
- **Golden**: fixtures asserting `ir_version` move 1.1 → 1.2.
- Full suite stays green (200 tests baseline + new).

## Demo acceptance (f1c47018, local-only)

Running `--tree-out` on the on-disk session renders: root `main` (25
requests) with ambient 109/249/20284 and `pstack:poteto-mode` marked
executed (alias `poteto-mode` shown); two child nodes under edges with
briefs 889 / 1000 EST and type `pstack:poteto-agent`; first child with
3 tool:read loads (5252 / 538 / 357 EST, sha1s rendered); second child
with the honest-empty line; coverage strip showing loads 3, unavailable
0, redundant 0, `joined_via_meta_only` 2, tier C 2 with the
"uncalibrated" note, and 1 identity merge with its alias. The session
and any quoted text stay local; nothing from it is committed.

## Security and local-only constraints (carried over, unchanged)

- The tool never instruments the agent; ASD measures transcripts after
  the fact.
- Session data and derived documents are processed and rendered
  locally; no upload, no external fetch from rendered pages (no-JS, no
  external assets — enforced by test).
- The web app constraint (127.0.0.1 only, no auth, never exposed) is
  untouched by this design.
