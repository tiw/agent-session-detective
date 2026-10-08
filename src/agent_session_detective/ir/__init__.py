# src/agent_session_detective/ir/__init__.py
"""Audit IR: one typed document per session, projecting wire events.

Public surface: ``build_audit_document`` (builder), the three analyses
(analyses, T12), ``IR_VERSION``/``BUCKETS``/``UNATTRIBUTED`` (schema).
"""

from .analyses import build_analyses  # noqa: F401
from .builder import build_audit_document  # noqa: F401
from .schema import BUCKETS, IR_VERSION, UNATTRIBUTED  # noqa: F401
