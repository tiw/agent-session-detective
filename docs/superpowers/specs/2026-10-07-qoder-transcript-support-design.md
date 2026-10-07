# Qoder Transcript Support Design

## Goal

Audit Qoder agent sessions with the same fact timeline, skill lifecycle, missed-trigger judgment, instruction-following evaluation, and report surfaces used for Kimi sessions.

## Scope

- Input: Qoder project transcripts at `~/.qoder/projects/<workspace>/<session>.jsonl`, or an explicitly supplied transcript path.
- Excluded input: `~/.qoder/logs/sessions/**/segments`, which records runtime diagnostics rather than the auditable conversation trace.
- Existing Kimi CLI and desktop wire formats remain supported without behavior changes.

## Architecture

Extend the parsing boundary in `wire.py`. Detect Qoder transcripts from their top-level record types, then normalize them into the existing `Event` and `Session` models. All downstream modules remain format-agnostic.

| Qoder record | Normalized event |
| --- | --- |
| `user.message.content` text | `TurnBegin` |
| `assistant.message.content` text | `ContentPart` text |
| `assistant.message.content` thinking | `ContentPart` think |
| `assistant.message.content` tool use | `ToolCall` |
| `user.message.content` tool result | `ToolResult` |
| assistant response usage | `UsageRecord` |

The parser converts ISO timestamps to Unix seconds, stores the transcript path in `Event.source`, and preserves the JSONL line number in `Event.seq`. It correlates a Qoder tool result through `sourceToolAssistantUUID` to the assistant tool-call record UUID, producing the existing call/result identifiers expected by the timeline.

## Entry Points and Discovery

The CLI accepts either a Qoder transcript file or a session directory. When no session is supplied, latest-session lookup considers the existing Kimi roots and the Qoder project-transcript root. Web discovery merges Kimi session directories with Qoder transcript files into one session list.

## Capability Boundaries

Qoder transcript data provides turns, assistant text and reasoning, tool calls/results, skill consumption, file reads, and response usage when present. It does not provide Kimi's context-token measurements, compaction events, or prompt/tool hashes. The report must present missing derived token-governance data as insufficient rather than infer or fabricate it.

## Error Handling

Malformed JSONL lines and unsupported metadata records are skipped. A transcript that yields no auditable events produces the existing no-events error. Kimi parsing behavior remains unchanged.

## Tests

Add a small fixed Qoder transcript fixture and automated tests for:

1. Event translation, timestamps, and source line numbers.
2. Tool-call/result correlation, skill load detection, and `SKILL.md` file-read detection.
3. Usage extraction and the absence of unsupported context-governance measures.
4. CLI/latest-session and web discovery of Qoder transcript paths.
5. Regression coverage for both existing Kimi layouts.
