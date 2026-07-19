"""Governance harness for queue-aiops — audit, policy, budget, undo, sanitize.

A self-contained, vendored governance layer. queue-aiops has NO dependency on
any external skill family — this package is its own copy of the harness:

  - ``@governed_tool`` — mandatory decorator on every MCP tool: policy pre-check,
    token/runaway budget guard, graduated-autonomy risk-tier gate, audit logging,
    and undo-token recording.
  - unified SQLite audit log under ``~/.queue-aiops/`` (override with
    ``QUEUE_AIOPS_HOME``).
  - ``sanitize`` — output hygiene (encoding-level defense) for API-returned text.

State lives under ``ops_home()`` (default ``~/.queue-aiops``).
"""

from queue_aiops.governance.audit import AuditEngine, get_engine
from queue_aiops.governance.budget import BudgetExceeded, BudgetTracker, get_budget
from queue_aiops.governance.decorators import PolicyDenied, governed_tool
from queue_aiops.governance.patterns import Pattern, PatternMatch, get_pattern_engine
from queue_aiops.governance.policy import TierDecision, get_policy_engine
from queue_aiops.governance.readonly import READ_ONLY_ENV, is_read_only
from queue_aiops.governance.sanitize import opt_str, sanitize
from queue_aiops.governance.undo import UndoStore, get_undo_store

__all__ = [
    "governed_tool",
    "sanitize",
    "opt_str",
    "is_read_only",
    "READ_ONLY_ENV",
    "PolicyDenied",
    "get_engine",
    "AuditEngine",
    "get_policy_engine",
    "TierDecision",
    "get_budget",
    "BudgetTracker",
    "BudgetExceeded",
    "get_undo_store",
    "UndoStore",
    "Pattern",
    "PatternMatch",
    "get_pattern_engine",
]
