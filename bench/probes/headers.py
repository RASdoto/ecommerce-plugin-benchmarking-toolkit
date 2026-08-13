"""
Profiler-header reader (Phase 3, [LT+P]).

Parses the response headers emitted by `bench-probe.php` when a request carries
`X-Bench: 1` (+ the shared secret). Turns per-request DB/memory internals into
numbers the load engine records inline.

Headers consumed:
  Server-Timing: app;dur=<ms>
  X-Bench-Query-Count, X-Bench-Query-Time, X-Bench-Slow-Queries,
  X-Bench-Peak-Memory (MB), X-Bench-PHP-Time (ms)
"""
from __future__ import annotations

import re
from typing import Mapping

_SERVER_TIMING_APP = re.compile(r"app;dur=([0-9.]+)")

# maps output metric name -> response header
_HEADER_MAP = {
    "query_count": "X-Bench-Query-Count",
    "query_time_ms": "X-Bench-Query-Time",
    "slow_queries": "X-Bench-Slow-Queries",
    "peak_mem_mb": "X-Bench-Peak-Memory",
    "php_time_ms": "X-Bench-PHP-Time",
}


def bench_request_headers(secret: str, dump: bool = False) -> dict:
    """Headers to send so the profiler activates for this request."""
    h = {"X-Bench": "1"}
    if secret:
        h["X-Bench-Secret"] = secret
    if dump:
        h["X-Bench-Dump"] = "1"
    return h


def parse_profiler_headers(headers: Mapping[str, str]) -> dict[str, float]:
    """Extract profiler metrics from a response's headers. Missing -> omitted."""
    out: dict[str, float] = {}

    # normalize header lookup to be case-insensitive
    lower = {k.lower(): v for k, v in headers.items()}

    st = lower.get("server-timing")
    if st:
        m = _SERVER_TIMING_APP.search(st)
        if m:
            out["server_ms"] = float(m.group(1))

    for metric, header in _HEADER_MAP.items():
        raw = lower.get(header.lower())
        if raw is None:
            continue
        try:
            out[metric] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def profiler_available(headers: Mapping[str, str]) -> bool:
    lower = {k.lower(): v for k, v in headers.items()}
    return "x-bench-query-count" in lower or "server-timing" in lower
