#!/usr/bin/env python3
"""Project an audit-IR call onto Qoder's live /context vocabulary.

Qoder's built-in context view (CLI/SDK getContextUsage, surfaced as
/context) reports category percentages for system_prompt, tools,
messages, skills, memory and free_space, plus per-skill
percentageOfContext. It is snapshot-only and never persisted - 0 hits
across 25 real transcripts. This script therefore implements the
calibration procedure from docs/calibration.md: run a live session,
capture a /context snapshot, then run this script on the same session's
transcript and compare the two share vectors to bound the offline
estimator's error. It is a documented validation procedure, not a
runtime dependency, and it never runs in CI.

Mapping (spec, Calibration section):
  system_prompt + tools -> unattributed
  messages             -> {user, assistant, tool}
  skills               -> skill
  memory               -> inject(memory)
  free_space           -> window remainder (1 - anchor / context_window)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_session_detective.cli import _detect_adapter  # noqa: E402
from agent_session_detective.ir import build_audit_document  # noqa: E402
from agent_session_detective.wire import load_session  # noqa: E402

LIVE_CATEGORIES = (
    "system_prompt+tools",
    "messages",
    "skills",
    "memory",
    "free_space",
)


def ir_window_shares(call, context_window, items_by_id):
    anchor = call.input.anchor_tokens
    if anchor is None:
        raise ValueError("call %s has no anchor tokens" % call.call_id)
    if not context_window or context_window <= 0:
        raise ValueError("context window is unknown for %s" % call.call_id)
    buckets = call.input.buckets
    memory_tokens = sum(
        ref.tokens_est
        for ref in call.input.item_refs
        if items_by_id[ref.item_id].kind == "memory"
    )
    return {
        "system_prompt+tools": call.input.unattributed_tokens / context_window,
        "messages": (
            buckets.get("user", 0)
            + buckets.get("assistant", 0)
            + buckets.get("tool", 0)
        )
        / context_window,
        "skills": buckets.get("skill", 0) / context_window,
        "memory": memory_tokens / context_window,
        "free_space": 1.0 - anchor / context_window,
    }


def _window_for(document, call, override):
    if override is not None:
        return override
    for agent in document.agents:
        if agent.agent_id == call.agent_id:
            return agent.context_window
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="calibrate_context",
        description="Compare an audit-IR call projection against a live /context snapshot.",
    )
    parser.add_argument("session", help="path to a session .jsonl transcript")
    parser.add_argument(
        "--call",
        default=None,
        help="call_id to project (default: the last request)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="context window override (wins over a recorded window)",
    )
    args = parser.parse_args(argv)

    source = Path(args.session).expanduser()
    session = load_session(source)
    document = build_audit_document(session, _detect_adapter(session, source))
    if not document.requests:
        print("no requests found in %s" % args.session, file=sys.stderr)
        return 1
    if args.call is None:
        call = document.requests[-1]
    else:
        call = next(
            (entry for entry in document.requests if entry.call_id == args.call),
            None,
        )
        if call is None:
            print("unknown call id %s" % args.call, file=sys.stderr)
            return 1
    window = _window_for(document, call, args.window)
    items_by_id = {item.item_id: item for item in document.items}
    try:
        shares = ir_window_shares(call, window, items_by_id)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("call %s  anchor %d / window %d" % (call.call_id, call.input.anchor_tokens, window))
    for name in LIVE_CATEGORIES:
        print("  %-20s %6.2f%%" % (name, shares[name] * 100.0))
    print("  %-20s %6.2f%%" % ("(unmapped buckets)", (1.0 - sum(shares.values())) * 100.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
