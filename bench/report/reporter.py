"""
Reporter (Phase 8). Aggregates the results store into a tidy CSV + JSON summary
and a machine-readable run_manifest.json (the agent's completion report).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..run.store import ResultStore


# columns emitted to dataset.csv (nested dicts flattened/omitted)
FLAT_COLS = [
    "platform", "operation", "entity", "volume", "concurrency", "status",
    "total_requests", "total_errors", "total_successes", "error_rate_pct",
    "total_time_s", "avg_ms", "throughput_rps",
    "p50_ms", "p90_ms", "p95_ms", "p99_ms",
    "mean_server_ms", "mean_query_count", "mean_query_time_ms",
    "mean_slow_queries", "mean_peak_mem_mb", "mean_php_time_ms",
]


class Reporter:
    def __init__(self, settings):
        self.s = settings
        self.store = ResultStore(settings.reports_dir / "results.jsonl")

    def write_csv(self) -> Path:
        rows = list(self.store.read_all())
        out = self.s.reports_dir / "dataset.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FLAT_COLS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                # pull server cpu/mem peaks up to flat columns if present
                srv = r.get("server") or {}
                if srv:
                    cpu = srv.get("cpu", {})
                    mem = srv.get("memory", {})
                    r = dict(r)
                    r["cpu_user_max"] = cpu.get("user", {}).get("max")
                    r["cpu_user_avg"] = cpu.get("user", {}).get("avg")
                    r["mem_used_max"] = mem.get("used", {}).get("max")
                    r["mem_used_avg"] = mem.get("used", {}).get("avg")
                w.writerow(r)
        return out

    def write_summary(self) -> Path:
        rows = [r for r in self.store.read_all() if r.get("status") == "ok"]
        summary = {"cells": len(rows), "by_platform": {}}
        for r in rows:
            p = r.get("platform", "?")
            summary["by_platform"].setdefault(p, {"cells": 0})
            summary["by_platform"][p]["cells"] += 1
        out = self.s.reports_dir / "summary.json"
        out.write_text(json.dumps(summary, indent=2, default=str))
        return out

    def write_manifest(self, run_stats: dict, bootstrap_state: dict | None = None) -> Path:
        rows = list(self.store.read_all())
        statuses = {}
        fallbacks = {}
        env = {}
        for r in rows:
            statuses[r.get("status", "?")] = statuses.get(r.get("status", "?"), 0) + 1
        if bootstrap_state:
            for site, st in bootstrap_state.items():
                if isinstance(st, dict):
                    fallbacks[site] = st.get("fallbacks", [])
                    if st.get("env"):
                        env[site] = st["env"]
        manifest = {
            "run": run_stats,
            "result_statuses": statuses,
            "fallbacks": fallbacks,
            "environment": env,
        }
        # decide overall status
        if run_stats.get("failed", 0) == 0 and not any(fallbacks.values()):
            manifest["overall"] = "complete"
        elif run_stats.get("completed", 0) > 0:
            manifest["overall"] = "complete-with-gaps"
        else:
            manifest["overall"] = "blocked"
        out = self.s.reports_dir / "run_manifest.json"
        out.write_text(json.dumps(manifest, indent=2, default=str))
        return out

    def build_all(self, run_stats: dict, bootstrap_state: dict | None = None) -> dict:
        return {
            "csv": str(self.write_csv()),
            "summary": str(self.write_summary()),
            "manifest": str(self.write_manifest(run_stats, bootstrap_state)),
        }
