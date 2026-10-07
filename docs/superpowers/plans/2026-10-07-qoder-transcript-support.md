# Qoder Transcript Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Qoder project-transcript parsing and discovery while preserving the existing Kimi audit behavior.

**Architecture:** Extend `wire.py` with an input-format detector and Qoder-to-`Event` translator, so timeline, judge, token stats, and report rendering continue to consume a single model. Update CLI and web discovery to treat a Qoder transcript file as a first-class session source; leave absent Qoder context/compaction/hash facts absent.

**Tech Stack:** Python 3.9+, standard-library `unittest`, JSONL, existing stdlib HTTP UI.

---

## File structure

- Modify `src/agent_session_detective/wire.py`: detect and parse Qoder project transcripts; discover the latest Qoder transcript.
- Modify `src/agent_session_detective/cli.py`: accept a transcript path, select the latest source across Kimi/Qoder roots, and select a non-colliding Qoder report path.
- Modify `src/agent_session_detective/web.py`: discover, fingerprint, and audit transcript files alongside Kimi session directories.
- Modify `src/agent_session_detective/__init__.py`: state that Qoder transcript audit is supported.
- Modify `README.md`: document Qoder input path, default discovery, and metric availability boundary.
- Create `tests/fixtures/qoder-transcript.jsonl`: minimal representative Qoder trace.
- Create `tests/test_wire.py`: parser, correlation, source-line, malformed-line, and discovery coverage.
- Create `tests/test_cli.py`: explicit Qoder file and default-output coverage.
- Create `tests/test_web.py`: Qoder discovery and fingerprint coverage.

### Task 1: Create the Qoder transcript fixture and parser tests

**Files:**
- Create: `tests/fixtures/qoder-transcript.jsonl`
- Create: `tests/test_wire.py`
- Modify: `src/agent_session_detective/wire.py:199-228`

- [ ] **Step 1: Write the fixture with one turn, a Skill call/result, a SKILL.md Read call/result, text, thinking, and usage**

```json
{"type":"user","uuid":"u1","timestamp":"2026-10-07T08:00:00.000Z","message":{"content":[{"type":"text","text":"audit this"}]}}
{"type":"assistant","uuid":"a1","timestamp":"2026-10-07T08:00:01.000Z","message":{"content":[{"type":"thinking","thinking":"inspect trace"},{"type":"tool_use","id":"tool-skill","name":"Skill","input":{"skill":"superpowers:brainstorming"}},{"type":"tool_use","id":"tool-read","name":"Read","input":{"file_path":"/tmp/demo/SKILL.md"}}],"usage":{"input_tokens":120,"output_tokens":30,"cache_read_input_tokens":80,"cache_creation_input_tokens":0}}}
{"type":"user","uuid":"r1","sourceToolAssistantUUID":"a1","timestamp":"2026-10-07T08:00:02.000Z","message":{"content":[{"type":"tool_result","tool_use_id":"tool-skill","content":"skill instructions"},{"type":"tool_result","tool_use_id":"tool-read","content":"plain skill file"}]}}
```

- [ ] **Step 2: Add failing parser tests**

```python
class QoderTranscriptTests(unittest.TestCase):
    def test_loads_qoder_transcript_into_existing_event_model(self):
        source = FIXTURES / "qoder-transcript.jsonl"
        session = load_session(source)
        self.assertEqual([e.type for e in session.events], [
            "TurnBegin", "ContentPart", "ToolCall", "ToolCall", "UsageRecord", "ToolResult", "ToolResult",
        ])
        self.assertEqual(session.events[0].payload["user_input"][0]["text"], "audit this")
        self.assertEqual(session.events[1].payload, {"type": "think", "think": "inspect trace"})
        self.assertEqual(session.events[2].payload["id"], "tool-skill")
        self.assertEqual(session.events[-2].payload["tool_call_id"], "tool-skill")
        self.assertEqual(session.events[4].payload["input_cache_read"], 80)
        self.assertEqual(session.events[0].source, source)
        self.assertEqual(session.events[0].seq, 1)
```

- [ ] **Step 3: Run the parser test to verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.test_wire.QoderTranscriptTests.test_loads_qoder_transcript_into_existing_event_model -v`

Expected: FAIL because `load_session()` only looks for Kimi `wire.jsonl` files.

- [ ] **Step 4: Implement Qoder format detection and translation in `wire.py`**

```python
def _qoder_timestamp(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _qoder_content_events(record: dict) -> Iterator[tuple[str, dict]]:
    message = record.get("message") or {}
    for part in message.get("content") or []:
        if part.get("type") == "text":
            yield "ContentPart", {"type": "text", "text": part.get("text", "")}
        elif part.get("type") == "thinking":
            yield "ContentPart", {"type": "think", "think": part.get("thinking", "")}
        elif part.get("type") == "tool_use":
            yield "ToolCall", {"id": part.get("id"), "function": {
                "name": part.get("name"),
                "arguments": json.dumps(part.get("input") or {}, ensure_ascii=False),
            }}
```

Implement the matching user-text, user-tool-result, and usage branches with the same normalized payload names. For a user tool result, emit one `ToolResult` per content item and use `tool_use_id` as `tool_call_id`; Qoder's `sourceToolAssistantUUID` remains unnecessary after the explicit call ID is available. Add `parse_qoder_transcript(path)` that skips malformed/metadata records, yields normalized events, and uses original JSONL line numbers.

- [ ] **Step 5: Route file inputs through the Qoder parser**

```python
def load_session(source: Path) -> Session:
    source = Path(source)
    if source.is_file():
        session = Session(directory=source.parent)
        session.events.extend(parse_qoder_transcript(source, origin="main"))
        session.events.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.seq))
        return session
    # retain the existing Kimi directory parsing below
```

- [ ] **Step 6: Run the parser tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_wire -v`

Expected: PASS.

### Task 2: Verify timeline and unavailable-metric behavior

**Files:**
- Modify: `tests/test_wire.py`
- Modify: `src/agent_session_detective/wire.py:26-73`

- [ ] **Step 1: Add failing behavioral tests using existing downstream modules**

```python
def test_qoder_skill_and_file_read_are_detected(self):
    timeline = build_timeline(load_session(FIXTURES / "qoder-transcript.jsonl"))
    self.assertEqual(timeline.skill_names(), ["superpowers:brainstorming"])
    self.assertEqual([r.skill_name for r in timeline.file_reads], ["demo"])


def test_qoder_without_context_measurements_has_insufficient_growth(self):
    session = load_session(FIXTURES / "qoder-transcript.jsonl")
    stats = build_token_stats(session, build_timeline(session))
    self.assertEqual(stats.output_total, 30)
    self.assertEqual(stats.cache_read_total, 80)
    self.assertTrue(stats.growth_verdict.startswith("insufficient data"))
    self.assertEqual(stats.hash_runs, [])
```

- [ ] **Step 2: Run the behavioral tests to verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.test_wire.QoderTimelineTests -v`

Expected: FAIL until Qoder tool results use `return_value.output` and usage uses the established `UsageRecord` payload fields.

- [ ] **Step 3: Complete Qoder payload normalization**

```python
yield "ToolResult", {
    "tool_call_id": part.get("tool_use_id"),
    "return_value": {"output": part.get("content", ""), "is_error": False},
}
yield "UsageRecord", {
    "input_other": usage.get("input_tokens", 0),
    "output": usage.get("output_tokens", 0),
    "input_cache_read": usage.get("cache_read_input_tokens", 0),
    "input_cache_creation": usage.get("cache_creation_input_tokens", 0),
    "model": "",
}
```

Keep `StatusUpdate`, `TurnTokens`, `LLMRequest`, and compaction absent for Qoder; the existing statistics code then reports only directly evidenced values.

- [ ] **Step 4: Run the behavioral tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_wire -v`

Expected: PASS.

### Task 3: Add CLI discovery and explicit-file support

**Files:**
- Modify: `src/agent_session_detective/wire.py:220-228`
- Modify: `src/agent_session_detective/cli.py:19-73,122-124`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Add failing latest-Qoder and CLI tests**

```python
def test_find_latest_qoder_transcript_returns_newest_jsonl(self):
    newest = find_latest_qoder_transcript(self.projects_root)
    self.assertEqual(newest.name, "new.jsonl")

@patch("agent_session_detective.cli.render_report", return_value="<html></html>")
def test_cli_accepts_qoder_transcript_file(render_report):
    rc = main([str(FIXTURES / "qoder-transcript.jsonl"), "--no-judge"])
    self.assertEqual(rc, 0)
    self.assertTrue((FIXTURES / "qoder-transcript.skill-audit.html").exists())
```

- [ ] **Step 2: Run the CLI tests to verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.test_cli -v`

Expected: FAIL because CLI validates only Kimi directory layouts.

- [ ] **Step 3: Implement source selection**

```python
DEFAULT_QODER_SESSIONS_ROOT = "~/.qoder/projects"


def find_latest_qoder_transcript(base: Path) -> Optional[Path]:
    candidates = list(base.glob("*/*.jsonl"))
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
```

In `cli.py`, accept a source when it is either a `.jsonl` file or a Kimi directory containing either supported wire path. With no positional source, compare the mtimes of `find_latest_session(Path(args.sessions_root).expanduser())` and `find_latest_qoder_transcript(Path(DEFAULT_QODER_SESSIONS_ROOT).expanduser())`. For a Qoder file default output use `source.with_suffix(".skill-audit.html")`; retain `<session-dir>/skill-audit.html` for Kimi directories.

- [ ] **Step 4: Run the CLI tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_cli -v`

Expected: PASS.

### Task 4: Extend web discovery, cache fingerprinting, and UI labels

**Files:**
- Modify: `src/agent_session_detective/web.py:28-73,287-317,464-490`
- Modify: `tests/test_web.py`

- [ ] **Step 1: Add failing discovery and fingerprint tests**

```python
def test_discover_sessions_includes_qoder_transcript(self):
    sessions = discover_sessions([str(self.kimi_root), str(self.qoder_projects_root)])
    qoder = next(s for s in sessions if s["path"].endswith("qoder-transcript.jsonl"))
    self.assertEqual(qoder["workspace"], "example-workspace")
    self.assertEqual(qoder["id"], "qoder-transcript")


def test_fingerprint_uses_qoder_transcript_file(self):
    self.assertNotEqual(fingerprint(str(self.transcript), "no-judge"), "0.000:0:no-judge:v2")
```

- [ ] **Step 2: Run the web tests to verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.test_web -v`

Expected: FAIL because discovery only globs `wire.jsonl` and fingerprint only reads wire paths.

- [ ] **Step 3: Generalize source metadata without parsing logs**

```python
DEFAULT_ROOTS = ["~/.kimi-code/sessions", "~/.kimi/sessions", "~/.qoder/projects"]

if root.name == "projects":
    for transcript in base.glob("*/*.jsonl"):
        found[str(transcript)] = {
            "id": transcript.stem,
            "path": str(transcript),
            "workspace": transcript.parent.name,
            "mtime": transcript.stat().st_mtime,
        }
```

Update `fingerprint()` to use `[base]` when `base.is_file()`, otherwise preserve the existing Kimi wire collection. Change the audit progress string from `parsing wire.jsonl` to `parsing session log`.

- [ ] **Step 4: Run the web tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_web -v`

Expected: PASS.

### Task 5: Document support and run regressions

**Files:**
- Modify: `README.md:49-62,114-129,171-187,237-246`
- Modify: `src/agent_session_detective/__init__.py:1-6`

- [ ] **Step 1: Add README examples and format table row**

```markdown
# Audit a Qoder transcript directly
PYTHONPATH=src python3 -m agent_session_detective \
  ~/.qoder/projects/<workspace>/<session>.jsonl --no-judge
```

Document that Qoder records tool and conversation facts plus usage when present, but not context measurements, compactions, or prompt hashes; those report fields remain insufficient/empty rather than inferred.

- [ ] **Step 2: Update the package description**

```python
"""Audit Kimi Code and Qoder agent sessions against their skills."""
```

- [ ] **Step 3: Run the complete standard-library test suite**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: PASS with Qoder parser, CLI, web, and existing Kimi regression tests green.

- [ ] **Step 4: Run two smoke audits**

Run: `PYTHONPATH=src python3 -m agent_session_detective tests/fixtures/qoder-transcript.jsonl --no-judge`

Expected: writes `tests/fixtures/qoder-transcript.skill-audit.html` and reports the normalized Qoder facts.

Run: `PYTHONPATH=src python3 -m agent_session_detective <known-kimi-session> --no-judge`

Expected: writes the existing Kimi report with no parsing regression.

- [ ] **Step 5: Review the working tree and request a code review**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only the Qoder feature files, tests, and documentation changes are present.

## Self-review

- **Spec coverage:** Tasks 1-2 implement transcript parsing, timeline correlation, usage, and evidence-only metrics. Task 3 covers CLI input and latest-session selection. Task 4 covers Web discovery, cache identity, and audit status. Task 5 documents the feature and verifies Qoder and Kimi behavior.
- **Placeholder scan:** No deferred work or unspecified test cases remain.
- **Type consistency:** Every Qoder tool call uses existing `ToolCall.id`, `function.name`, and JSON-string `function.arguments`; every result uses existing `ToolResult.tool_call_id` and `return_value.output`; usage uses existing `UsageRecord` field names.
