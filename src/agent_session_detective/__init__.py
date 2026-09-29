"""Audit agent sessions against their skills.

Reconstructs the skill lifecycle from a Kimi Code session log (what loaded,
when, at what context cost, when compaction evicted it), judges which skills
should have triggered but never did, and renders an interactive HTML report.
"""

__version__ = "0.1.0"
