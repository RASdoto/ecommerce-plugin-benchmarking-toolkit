"""
Metrics aggregation — percentiles, error buckets, summary builder.
Replaces the stats the Node `loadtest` library produced (parity P21).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0])
    # nearest-rank method (matches loadtest's integer percentile reporting)
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return round(sorted_vals[f])
    d = sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])
    return round(d)


@dataclass
class LoadResult:
    url: str
    total_requests: int = 0
    total_errors: int = 0
    total_time_seconds: float = 0.0
    timings_ms: list[float] = field(default_factory=list)
    error_codes: dict[str, int] = field(default_factory=dict)
    # extended per-request profiler aggregates (Phase 3)
    profiler: dict[str, list[float]] = field(default_factory=dict)

    # ---- derived ----------------------------------------------------------
    @property
    def total_successes(self) -> int:
        return self.total_requests - self.total_errors

    @property
    def average_ms(self) -> float:
        if not self.total_requests:
            return 0.0
        return (self.total_time_seconds * 1000.0) / self.total_requests

    @property
    def throughput_rps(self) -> float:
        if self.total_time_seconds <= 0:
            return 0.0
        return self.total_requests / self.total_time_seconds

    def percentiles(self) -> dict[str, float]:
        s = sorted(self.timings_ms)
        return {p: percentile(s, int(p)) for p in ("50", "90", "95", "99")}

    def profiler_means(self) -> dict[str, float]:
        out = {}
        for key, vals in self.profiler.items():
            if vals:
                out[key] = round(sum(vals) / len(vals), 2)
        return out

    def as_row(self) -> dict[str, Any]:
        """Flat metric dict for the results store / dataset.csv."""
        p = self.percentiles()
        row = {
            "url": self.url,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "total_successes": self.total_successes,
            "error_rate_pct": round(
                (self.total_errors / self.total_requests * 100.0) if self.total_requests else 0.0, 3
            ),
            "total_time_s": round(self.total_time_seconds, 3),
            "avg_ms": round(self.average_ms, 2),
            "throughput_rps": round(self.throughput_rps, 2),
            "p50_ms": p["50"],
            "p90_ms": p["90"],
            "p95_ms": p["95"],
            "p99_ms": p["99"],
            "error_codes": dict(self.error_codes),
        }
        for k, v in self.profiler_means().items():
            row[f"mean_{k}"] = v
        return row
