# src/agent_session_detective/ir/spans.py
"""Positional request spans + anchor runs + cross-verification.

Span rule (DERIVED): a user record flushes any open run to ``ungroupable``;
assistant records accumulate; a record with positive usage closes the span.
Meta records are invisible to the rule.

Anchor run rule (FACT): maximal runs of assistant records sharing one non-null
``(request_hash, request_id)``. User records break adjacency; meta and
anchorless assistant records neither join nor break — they are simply not part
of any run. Verification compares run and span seq lists at the run's first
seq; a disagreement is a coverage fact, not an error to hide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .records import WireRecord


@dataclass
class RequestSpan:
    records: List[WireRecord] = field(default_factory=list)

    @property
    def first_seq(self) -> int:
        return self.records[0].seq

    @property
    def last_seq(self) -> int:
        return self.records[-1].seq

    @property
    def n_records(self) -> int:
        return len(self.records)

    @property
    def usage_record(self) -> Optional[WireRecord]:
        last = self.records[-1]
        if last.has_positive_usage:
            return last
        return None


@dataclass
class AnchorRun:
    request_id: str
    request_hash: Optional[str]
    records: List[WireRecord] = field(default_factory=list)


def positional_spans(records: List[WireRecord]) -> Tuple[List[RequestSpan], List[WireRecord]]:
    spans: List[RequestSpan] = []
    ungroupable: List[WireRecord] = []
    pending: List[WireRecord] = []

    def flush() -> None:
        if pending:
            ungroupable.extend(pending)
            pending.clear()

    for record in records:
        if record.kind == "user":
            flush()
        elif record.kind == "assistant":
            pending.append(record)
            if record.has_positive_usage:
                spans.append(RequestSpan(list(pending)))
                pending.clear()
    flush()
    return spans, ungroupable


def anchor_runs(records: List[WireRecord]) -> List[AnchorRun]:
    runs: List[AnchorRun] = []
    broken = True
    for record in records:
        if record.kind == "user":
            broken = True
            continue
        if record.kind != "assistant":
            continue
        request_id = record.request_id
        request_hash = record.request_hash
        if request_id is None or request_hash is None:
            continue
        if runs and not broken:
            last = runs[-1]
            if last.request_id == request_id and last.request_hash == request_hash:
                last.records.append(record)
                continue
        runs.append(AnchorRun(request_id, request_hash, [record]))
        broken = False
    return runs


def verify_against_anchors(
    spans: List[RequestSpan], runs: List[AnchorRun]
) -> Tuple[int, List[dict]]:
    by_first_seq = {span.first_seq: span for span in spans}
    checked = 0
    disagreements: List[dict] = []
    for run in runs:
        span = by_first_seq.get(run.records[0].seq)
        if span is None:
            continue
        checked += 1
        run_seqs = [record.seq for record in run.records]
        span_seqs = [record.seq for record in span.records]
        if run_seqs != span_seqs:
            disagreements.append(
                {
                    "request_id": run.request_id,
                    "run_seqs": run_seqs,
                    "span_seqs": span_seqs,
                }
            )
    return checked, disagreements
