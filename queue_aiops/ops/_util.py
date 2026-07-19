"""Shared helpers for the queue ops modules.

redis (INFO field maps) and rabbitmq (management-API JSON) return the same
broker concepts under different shapes. The ops modules stay platform-neutral
by asking the platform for paths/rows (see :mod:`queue_aiops.platform`) and by
reading fields through :func:`pick` / :func:`num`, which tolerate missing or
oddly-typed cells. All broker text reaches the caller only after ``sanitize()``
via ``s``.
"""

from __future__ import annotations

from typing import Any

from queue_aiops.governance import opt_str, sanitize


def as_obj(data: Any) -> dict:
    """Return ``data`` as a dict (empty dict if it isn't one)."""
    return data if isinstance(data, dict) else {}


def s(value: Any, limit: int = 256) -> str:
    """Sanitize an arbitrary value to a bounded, injection-safe string."""
    return sanitize(str(value if value is not None else ""), limit)


def opt(value: Any, limit: int = 256) -> str | None:
    """Sanitize an *optional* field, preserving the difference between absent and empty.

    Companion to :func:`s`, which folds ``None`` into ``""``. Broker payloads are
    full of genuinely-absent values: a queue with no consumers has no
    ``consumer_tag``, a Redis replica reports no ``master_link_status`` when it
    is a primary, and RabbitMQ omits ``idle_since`` for an active queue. "The
    broker has nothing to report here" is a different fact from "the value is
    blank", and only the second should read as empty.

    Use this for anything read out of a response row; keep :func:`s` for values
    that always exist (an exception message, a caller-supplied queue name).
    """
    return opt_str(value, limit)


def pick(row: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None value among ``keys`` (else ``default``)."""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def num(value: Any) -> float:
    """Coerce a numeric cell to float; 0.0 when absent/non-numeric.

    Use this only for genuinely fractional values (rates, ratios, percentages).
    For integer quantities — key counts, byte counts, client counts, whole
    seconds — use :func:`as_int`, which keeps them integers.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def as_int(value: Any) -> int:
    """Coerce an integer quantity (counts, bytes, whole seconds) to ``int``.

    Redis ``INFO`` and the RabbitMQ management API hand these back as strings;
    routing them through :func:`num` rendered them as ``202.0`` keys or
    ``1.0`` connected clients — arithmetically right but semantically wrong,
    since a count cannot be fractional. A reader (human or model) should not
    have to wonder whether a ``.0`` means the value was rounded.

    Non-numeric/absent yields ``0`` to match :func:`num`'s contract for these
    always-present INFO fields.
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def rate(row: Any, default: float = 0.0) -> float:
    """Read a management-API ``*_details.rate`` cell (``{"rate": 1.2}``)."""
    if isinstance(row, dict):
        return num(row.get("rate", default))
    return default


def pct(part: float, whole: float) -> float:
    """Percentage (1 decimal); 0.0 when the denominator is 0/absent."""
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)
