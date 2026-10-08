# src/agent_session_detective/ir/estimator.py
"""Token estimator, versioned (R7).

Delegates to ``timeline.estimate_tokens`` — one estimator in the codebase,
one version string in the IR. Non-string input estimates to 0 so that
optionally-present payload fields degrade to "absent" instead of crashing.
"""

from __future__ import annotations

from ..timeline import estimate_tokens

ESTIMATOR_VERSION = "cjk-1.0"


def estimate(text) -> int:
    if not isinstance(text, str) or not text:
        return 0
    return estimate_tokens(text)
