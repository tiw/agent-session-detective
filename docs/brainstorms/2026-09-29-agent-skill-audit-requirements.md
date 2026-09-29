---
date: 2026-09-29
topic: agent-skill-audit
---

## Summary

A CLI tool that audits a single Kimi Code session against its skills: it reconstructs a fact timeline of the skill lifecycle from the session log (what loaded, when, how much context it occupied, when compaction evicted it), uses LLM counterfactual judgment to find skills that should have triggered but never did, and renders the findings as a self-contained interactive HTML report where every conclusion links back to quoted evidence.

## Problem Frame

The user runs agents with a growing library of skills and has repeatedly observed agents not working according to skill descriptions. Three failure modes have been seen in practice: a skill whose trigger condition clearly matched but was never loaded; a skill that loaded with incomplete or wrong content; and a skill whose content was in context but whose instructions the model did not follow. Today, diagnosing any of these means manually grepping raw session logs — the data exists but there is no way to see the skill's journey through a conversation. The tool makes that journey inspectable after the fact, per session, starting from the failure mode that is hardest to see by hand: skills that never triggered at all.

## Key Decisions

- **Post-hoc log analysis over runtime instrumentation.** The tool reads session logs after the run. No hooks or plugins into the agent runtime are needed, and past sessions remain auditable. Runtime precision is traded away; the log is the source of truth.
- **Kimi Code only in v1.** One log format is understood deeply before any generalization. Claude Code and a framework-agnostic intermediate format are deferred.
- **Structured facts plus LLM judgment, never LLM alone.** Every load, token, and compaction fact is extracted deterministically from the log. LLM judgment is applied only to the counterfactual trigger question, and every judgment must cite its evidence. Facts are checkable; judgments are labeled as judgments.
- **Counterfactual LLM judging over declarative trigger rules.** Skill authors write descriptions, not machine-readable triggers. Maintaining per-skill rules would miss vague or evolving descriptions; asking an LLM "at this decision point, should this skill have triggered?" handles them as written.
- **Interactive HTML as the report.** The audit output is a single-file HTML page with an expandable timeline and side-by-side evidence, chosen over terminal output because timeline navigation and evidence comparison are the core value.
- **Narrow v1.** Single session, fact timeline, trigger judgment, HTML report. The other observed failure modes get fact presentation without judgment in v1.

## Requirements

**Log ingestion**

- R1. The tool accepts a Kimi Code session directory (or locates the most recent session for the current project) and parses its `wire.jsonl` event stream: turns, tool calls, tool results, status updates, and compaction events.
- R2. Subagent wire logs in the session are parsed and attributed to their parent session, so skills loaded inside a subagent appear in the parent's timeline.

**Fact timeline**

- R3. The timeline records every skill lifecycle event with timestamp: loaded (with the loaded content captured at load time), active, and evicted (when a compaction event plausibly dropped it from context).
- R4. Each load event records its estimated context cost in tokens, read from the status updates that bracket it.
- R5. Facts are distinguished from judgments throughout the report: timeline entries are log-derived, never inferred.

**Trigger judgment**

- R6. The tool builds the catalog of available skills from the local skill directories active for the audited session, including each skill's name, description, and trigger wording.
- R7. For each skill that never loaded, the tool asks the LLM judge the counterfactual question at the relevant decision points: given the conversation up to that point, should this skill have been triggered? Each answer carries the judge's rationale and verbatim evidence quotes from the log.
- R8. Judgments are labeled with confidence, and a judgment without cited evidence is treated as a tool defect, not a finding.

**Report**

- R9. The report is a single HTML file with no server or external assets. The timeline is expandable per event; findings link to the exact log lines they rest on.
- R10. Skill content shown in the report is the content captured at load time, so the report stays accurate even if the skill file changes later.

## Key Flows

- F1. Audit a session
  - **Trigger:** User points the CLI at a session (or defaults to the latest one) and runs the audit.
  - **Steps:** Parse the event stream → build the fact timeline → assemble the not-loaded skill catalog → run counterfactual trigger judging → render the HTML report.
  - **Outcome:** A report opening on the timeline, with a findings section listing missed-trigger candidates.
- F2. Examine a missed-trigger finding
  - **Trigger:** User expands a finding in the report.
  - **Steps:** See the decision point in the conversation, the candidate skill's description, the judge's rationale with confidence, and quoted evidence from the log side by side.
  - **Outcome:** The user can accept the finding, dismiss it, or open the referenced log segment directly.

## Acceptance Examples

- AE1. **Covers R3, R4.** A session loads a skill mid-conversation and later compacts. The timeline shows the load with captured content and token cost, marks the compaction event, and marks the skill as evicted from that point on — even though the log never says "skill dropped" explicitly.
- AE2. **Covers R7.** A session discusses committing and pushing work while a commit-and-PR skill exists in the catalog but never loads. The findings section lists that skill with the decision point, the judge's rationale, and quoted user text as evidence.

## Success Criteria

- A known real session with a missed trigger produces a correct finding with checkable evidence.
- Every non-fact claim in the report traces to a visible log quote; a reviewer can verify any finding without rerunning the tool.
- The report answers "was this session following its skills?" in under a minute of reading for a typical session.

## Scope Boundaries

Deferred for later:

- Multi-session batch scanning and cross-session statistics.
- Skill content integrity: truncation, missing bundled files, stale versions.
- Semantic compliance judgment for loaded skills ("in context but not followed").
- Claude Code support and a framework-agnostic log format.
- Runtime instrumentation alongside post-hoc analysis.
- Declarative per-skill trigger rules as a supplement to LLM judging.

## Dependencies / Assumptions

- Kimi Code writes session logs to `~/.kimi/sessions/<project>/<session>/wire.jsonl`. Verified present; sampled logs contain turns, tool calls, status updates with context token counts, and compaction events. No dedicated skill-load event type exists in these logs (sampled through May 2026).
- Unverified assumption: when a skill loads, it appears in the log as a Skill tool call whose result carries the skill body. Historical logs contain no Skill tool call samples, so this has not been confirmed against real data.
- The skill catalog is read from the user's skill directories (user-level `~/.agents/skills` plus project-level skills listed in the project's `AGENTS.md`).
- The LLM judge uses model credentials already configured on the user's machine; no new API onboarding.

## Outstanding Questions

- Deferred to planning: which model serves as the judge, and how token cost is estimated for loaded skill content.
- First implementation task: confirm the skill-load recording path. The tool's log parser assumes skills appear as Skill tool calls; verify by invoking a skill in a live session and inspecting the resulting `wire.jsonl`, then adapt the parser to whatever is actually recorded.
