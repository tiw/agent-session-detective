"""CLI entry point: audit a Kimi Code session and render the HTML report."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path
from typing import List

from .catalog import load_catalog
from .judge import Judge, judge_session
from .report import render_report
from .timeline import build_timeline
from .wire import find_latest_session, load_session

DEFAULT_SESSIONS_ROOT = "~/.kimi/sessions"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="asd",
        description="Audit an agent session against its skills and render an HTML report.",
    )
    parser.add_argument(
        "session",
        nargs="?",
        default=None,
        help="Session directory (containing wire.jsonl). Defaults to the most recent session.",
    )
    parser.add_argument(
        "--sessions-root",
        default=DEFAULT_SESSIONS_ROOT,
        help="Root used to locate the most recent session (default: %(default)s).",
    )
    parser.add_argument("--skills-dir", action="append", default=None,
                        help="Extra skill directory to include in the catalog (repeatable).")
    parser.add_argument("--out", default=None,
                        help="Report output path (default: <session>/skill-audit.html).")
    parser.add_argument("--open", action="store_true", help="Open the report in a browser.")
    parser.add_argument("--no-judge", action="store_true", help="Skip LLM trigger judging.")
    parser.add_argument("--expect", default=None,
                        help="Comma-separated skill names expected to be consumed "
                             "(loaded or file-read); rendered as a hit/miss checklist.")
    parser.add_argument("--judge-limit", type=int, default=None,
                        help="Judge only the first N unloaded skills (cheaper runs).")
    args = parser.parse_args(argv)

    session_dir = Path(args.session).expanduser() if args.session else find_latest_session(
        Path(args.sessions_root).expanduser()
    )
    has_wire = session_dir is not None and (
        (session_dir / "wire.jsonl").exists()
        or (session_dir / "agents" / "main" / "wire.jsonl").exists()
    )
    if session_dir is None or not has_wire:
        print("No session found. Pass a session directory or check --sessions-root.", file=sys.stderr)
        return 1

    session = load_session(session_dir)
    if not session.events:
        print("No events parsed from %s" % session_dir, file=sys.stderr)
        return 1

    timeline = build_timeline(session)
    catalog = load_catalog(extra_dirs=args.skills_dir)

    judge = None if args.no_judge else Judge.from_env()
    judgments = []
    if judge:
        print("Judging %d unconsumed skills (model: %s)..." % (
            min(args.judge_limit or len(catalog), max(len(catalog) - len(timeline.consumed_skill_names()), 0)),
            judge.model,
        ), file=sys.stderr)
        judgments = judge_session(timeline, catalog, judge, limit=args.judge_limit)
    else:
        print("Judge not configured; facts only. See ASD_JUDGE_* env vars.", file=sys.stderr)

    report = render_report(
        session, timeline, judgments,
        judge_enabled=judge is not None,
        catalog_size=len(catalog),
        expected=_parse_expected(args.expect),
    )
    out_path = Path(args.out) if args.out else session_dir / "skill-audit.html"
    out_path.write_text(report, encoding="utf-8")
    print(out_path)
    if args.open:
        webbrowser.open(out_path.as_uri())
    return 0


def _parse_expected(raw: str) -> List[str]:
    return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


if __name__ == "__main__":
    sys.exit(main())
