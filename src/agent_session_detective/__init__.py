"""Audit Kimi Code and Qoder agent sessions against their skills.

Reconstructs the available skill lifecycle facts from a session log (what
loaded, when, and available context or compaction telemetry), judges which
skills should have triggered but never did, and renders an interactive HTML
report.
"""

__version__ = "0.1.0"
