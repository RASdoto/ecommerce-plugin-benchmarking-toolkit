"""
Progress helpers for the matrix runner.

Reads the heartbeat file the Runner emits + the results.jsonl store to render
a human-friendly snapshot of what's done, what's currently running, and a
naive ETA. Consumed by `bench progress`, `bench pause`, `bench resume`.
"""
from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..settings import Settings
from .store import ResultStore


def _load_heartbeat(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _age_seconds(iso: str) -> float:
    try:
        started = datetime.fromisoformat(iso)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds()
    except (ValueError, TypeError):
        return 0.0


def snapshot(settings: Settings) -> dict:
    """Return a dict describing the current run state."""
    hb_path = settings.reports_dir / "heartbeat.json"
    hb = _load_heartbeat(hb_path)

    store = ResultStore(settings.reports_dir / "results.jsonl")
    rows = list(store.read_all())

    # Compute plan-vs-done from actual results (source of truth), fall back
    # to heartbeat metadata when live state is unknown.
    done_ok = sum(1 for r in rows if r.get("status") == "ok")
    done_fail = sum(1 for r in rows if r.get("status") == "failed")
    done_skip = sum(1 for r in rows if str(r.get("status", "")).startswith("skipped"))
    done_total = len(rows)

    # Recompute planned cells from the runner's plan
    from .runner import Runner
    from ..core.config import load_app_config
    try:
        app_config = load_app_config(settings.config_json_path)
    except Exception:
        app_config = {}
    runner = Runner(settings=settings, app_config=app_config)
    planned = len(runner.plan_cells())

    pid = hb.get("pid")
    alive = _pid_alive(pid)
    state = hb.get("state") or ("finished" if done_total >= planned and planned else "idle")
    if not alive and state in ("running", "starting"):
        state = "stopped"

    current = hb.get("current") or None
    current_elapsed = None
    if current and current.get("started_at"):
        current_elapsed = _age_seconds(current["started_at"])

    # ETA — naive: avg cell wall time * remaining cells
    avg = hb.get("avg_cell_seconds")
    if not avg and done_total and hb.get("elapsed_seconds"):
        avg = hb["elapsed_seconds"] / max(done_total, 1)
    remaining = max(planned - done_total, 0)
    eta = (avg * remaining) if avg else None

    # Per-platform breakdown
    by_platform: dict[str, dict[str, int]] = {}
    for r in rows:
        plat = r.get("platform", "?")
        bucket = by_platform.setdefault(plat, {"ok": 0, "failed": 0, "skipped": 0})
        st = r.get("status", "")
        if st == "ok":
            bucket["ok"] += 1
        elif st == "failed":
            bucket["failed"] += 1
        elif st.startswith("skipped"):
            bucket["skipped"] += 1

    return {
        "pid": pid,
        "pid_alive": alive,
        "state": state,
        "planned": planned,
        "done": {
            "total": done_total,
            "ok": done_ok,
            "failed": done_fail,
            "skipped": done_skip,
        },
        "remaining": remaining,
        "percent": round(100 * done_total / planned, 1) if planned else 0.0,
        "current": current,
        "current_elapsed_seconds": current_elapsed,
        "avg_cell_seconds": avg,
        "eta_seconds": eta,
        "started_at": hb.get("started_at"),
        "finished_at": hb.get("finished_at"),
        "paused_at": hb.get("paused_at"),
        "by_platform": by_platform,
        "heartbeat_path": str(hb_path),
    }


def render(snap: dict) -> str:
    lines: list[str] = []
    state = snap["state"].upper()
    lines.append(f"== bench progress ==  state: {state}  pid: {snap.get('pid') or '-'}"
                 f"  alive: {snap.get('pid_alive')}")
    if snap.get("started_at"):
        lines.append(f"  started_at: {snap['started_at']}")
    if snap.get("paused_at"):
        lines.append(f"  paused_at:  {snap['paused_at']}")
    if snap.get("finished_at"):
        lines.append(f"  finished_at:{snap['finished_at']}")

    planned = snap["planned"] or 1
    done = snap["done"]["total"]
    bar_w = 30
    filled = int(bar_w * done / planned)
    bar = "[" + "#" * filled + "-" * (bar_w - filled) + "]"
    lines.append(f"  {bar}  {done}/{snap['planned']}  ({snap['percent']}%)")
    lines.append(f"  ok={snap['done']['ok']}  failed={snap['done']['failed']}"
                 f"  skipped={snap['done']['skipped']}  remaining={snap['remaining']}")

    if snap.get("avg_cell_seconds"):
        lines.append(f"  avg_cell={_fmt_duration(snap['avg_cell_seconds'])}"
                     f"  eta={_fmt_duration(snap['eta_seconds']) if snap.get('eta_seconds') else '?'}")

    cur = snap.get("current")
    if cur:
        c = cur["cell"]
        elapsed = snap.get("current_elapsed_seconds") or 0
        lines.append(
            f"  RUNNING #{cur['index']}: {c['platform']}/{c['operation']}  "
            f"vol={c['volume']}  conc={c['concurrency']}  "
            f"elapsed={_fmt_duration(elapsed)}"
        )
    else:
        lines.append("  RUNNING: (none)")

    if snap["by_platform"]:
        lines.append("  by platform:")
        for plat, b in sorted(snap["by_platform"].items()):
            lines.append(f"    {plat:12s} ok={b['ok']:3d}  failed={b['failed']:3d}"
                         f"  skipped={b['skipped']:3d}")

    return "\n".join(lines)


def pause(settings: Settings) -> dict:
    """Send SIGTERM to the running runner (graceful: finishes current cell)."""
    hb = _load_heartbeat(settings.reports_dir / "heartbeat.json")
    pid = hb.get("pid")
    if not pid:
        return {"ok": False, "reason": "no heartbeat / no pid recorded"}
    if not _pid_alive(pid):
        return {"ok": False, "reason": f"pid {pid} is not alive",
                "state": hb.get("state")}
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError as exc:
        return {"ok": False, "reason": f"kill failed: {exc}"}
    return {"ok": True, "pid": pid,
            "note": "SIGTERM sent — runner will finish current cell then exit."}
