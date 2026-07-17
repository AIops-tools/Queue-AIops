"""CLI package for queue-aiops.

Re-exports ``app`` so the pyproject entry point
``queue-aiops = "queue_aiops.cli:app"`` works unchanged.
"""

from queue_aiops.cli._root import app

__all__ = ["app"]
