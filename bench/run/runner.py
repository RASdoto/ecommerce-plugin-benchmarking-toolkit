"""
Matrix runner (Phase 8). Iterates platform x operation x volume x concurrency,
orchestrating per cell:
    ensure volume (seeder) -> start sysmon -> httpload (+profiler) -> stop sysmon
    -> dbprobe/browser extras -> append one result row.

Resumable via the ResultStore; each cell is written immediately.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..core.httpload import run_load_test
from ..core.metrics import LoadResult
from ..modules.registry import make_module
from ..report.logger import ReportLogger
from ..settings import Settings
from .store import ResultStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# operation -> (module_path, entity, kind)   kind in {read, write}
OPERATIONS = {
    "product-list": ("fluent-cart/products/list", "products", "read"),
    "product-create": ("fluent-cart/products/create", "products", "write"),
    "checkout": ("fluent-cart/order/create", "orders", "write"),
    # Woo / EDD equivalents are selected per platform in _module_for
}

# per-platform operation -> module path override
PLATFORM_OPS = {
    "fluentcart": {
        "checkout": "fluent-cart/order/create",
        "order-create": "fluent-cart/order/create",
        "product-create": "fluent-cart/products/create",
        "product-list": "fluent-cart/products/list",
    },
    "woo": {
        "order-create": "woo/order/create",
        "product-create": "woo/products/create",
        "product-list": "woo/products/list",
    },
    "edd": {
        "order-create": "edd/order/create",
        "customer-create": "edd/customer/Create",
        "product-create": "edd/product/create",
        "product-list": "edd/product/list",
    },
}

READ_OPS = {"product-list", "order-list", "search"}
WRITE_OPS = {"checkout", "order-create", "product-create", "customer-create"}
ENTITY_OF = {
    "checkout": "orders", "order-create": "orders",
    "product-create": "products", "customer-create": "customers",
    "product-list": "products", "order-list": "orders", "search": "products",
}


@dataclass
class Runner:
    settings: Settings
    app_config: dict
    heartbeat: Optional[callable] = None
    store: ResultStore = field(init=False)

    def __post_init__(self):
        self.store = ResultStore(self.settings.reports_dir / "results.jsonl")
        self.heartbeat_path = self.settings.reports_dir / "heartbeat.json"

    # ------------------------------------------------------------------
    def _write_heartbeat(self, payload: dict) -> None:
        """Atomically write the heartbeat file used by `bench progress`."""
        try:
            payload = {**payload, "last_updated": _now_iso()}
            tmp = self.heartbeat_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str))
            os.replace(tmp, self.heartbeat_path)
        except Exception:
            # heartbeat is best-effort; never let it kill a run
            pass

    # ------------------------------------------------------------------
    def _module_for(self, platform: str, operation: str):
        mp = PLATFORM_OPS.get(platform, {}).get(operation)
        if not mp:
            return None
        secret = self.settings.secret_resolver(platform)
        # config.json has a single global `base_url` (FluentCart site);
        # rebind it to the current platform's REST URL so Woo/EDD hit
        # the right host.
        site = self.settings.sites.get(platform)
        cfg = dict(self.app_config)
        if site is not None:
            cfg["base_url"] = site.rest_url
        return make_module(mp, cfg, self.settings.module_dir, secret=secret)

    def _volumes_for(self, entity: str) -> list[int]:
        m = self.settings.matrix
        if entity == "products":
            return m.get("product_volumes", [1000, 10000, 50000, 100000])
        if entity == "orders":
            return m.get("order_volumes", [100, 500, 1000, 2500, 5000])
        return m.get("customer_volumes", [100, 500, 1000, 2500, 5000])

    def _concurrency_for(self, operation: str) -> list[int]:
        m = self.settings.matrix
        if operation in READ_OPS:
            return m.get("read_concurrency", [1, 10, 25, 50, 100])
        return m.get("write_concurrency", [1, 5, 10, 25, 50])

    # ------------------------------------------------------------------
    def plan_cells(self) -> list[dict]:
        cells = []
        seen: set[str] = set()
        platforms = self.settings.matrix.get("platforms", list(self.settings.sites.keys()))
        operations = self.settings.matrix.get(
            "operations", ["product-list", "product-create", "order-create"]
        )
        for platform in platforms:
            for operation in operations:
                if platform not in PLATFORM_OPS or operation not in PLATFORM_OPS[platform]:
                    continue
                entity = ENTITY_OF.get(operation, "products")
                for volume in self._volumes_for(entity):
                    for conc in self._concurrency_for(operation):
                        cell = {
                            "platform": platform, "operation": operation,
                            "entity": entity, "volume": volume, "concurrency": conc,
                        }
                        key = self.store.cell_key(cell)
                        if key in seen:
                            continue
                        seen.add(key)
                        cells.append(cell)
        return cells

    # ------------------------------------------------------------------
    def run_cell(self, cell: dict) -> dict:
        platform = cell["platform"]
        operation = cell["operation"]
        volume = cell.get("volume", 0)
        row_seed = None

        # SEED -> MEASURE ordering (uniform for FC/Woo/EDD): ensure the
        # prerequisites/target volume exist before measuring. For order
        # placement, prereqs are VERIFIED but never auto-created (creating
        # products/customers here would muddy the matrix's volume tiers).
        try:
            from ..seed.factory import prepare_platform_data
            row_seed = prepare_platform_data(self.settings, platform, operation, volume)
        except Exception as exc:
            row_seed = {"seeded": False, "error": str(exc)}

        # Skip cells whose prereqs are missing — measuring them would produce
        # a wall of 4xx/5xx from the platform and pollute the results table.
        if row_seed and row_seed.get("seeded") is False \
                and operation in ("order-create", "checkout", "order-list"):
            row = dict(cell)
            row["seed"] = row_seed
            row["status"] = "skipped-prereq-missing"
            row["error"] = row_seed.get("error") or row_seed.get("reason")
            return row

        # FluentCart order-create: resolve real refs from the now-verified data
        # (resolve-only; prerequisites must already exist per the rule above).
        if platform == "fluentcart" and operation in ("order-create", "checkout"):
            try:
                from ..seed.fluentcart import ensure_fc_order_refs
                ensure_fc_order_refs(self.settings, "fluentcart", create_if_missing=False)
            except Exception:
                pass

        # WooCommerce order-create: resolve a real product id (the static
        # post.json hardcodes 91, which a catalogue reset removes).
        if platform == "woo" and operation == "order-create":
            try:
                from ..seed.woo import ensure_woo_order_refs
                ensure_woo_order_refs(self.settings)
            except Exception:
                pass

        module = self._module_for(platform, operation)
        row = dict(cell)
        if row_seed is not None:
            row["seed"] = row_seed
        if module is None:
            row["status"] = "skipped-no-module"
            return row

        site = self.settings.sites.get(platform)
        bench_secret = self.settings.bench_secret(platform)

        # cap write volume: total requests for a create op must not exceed
        # remaining headroom under the entity ceiling
        conc = cell["concurrency"]
        if operation in WRITE_OPS:
            max_requests = min(cell["volume"], conc * 20)  # bounded burst
        else:
            max_requests = conc * 10  # read ops: fixed request budget per cell

        spec = module.to_spec(max_requests_override=max_requests)
        spec.concurrency = conc

        # config.json contains some absolute URLs with stale hostnames
        # (e.g. edd.example.com, fc.example.com). credentials.yaml is
        # the source of truth for site hosts, so rewrite scheme+host on
        # any absolute URL to match the platform's configured site.
        if site is not None:
            from urllib.parse import urlsplit, urlunsplit
            site_parts = urlsplit(site.url)
            spec_parts = urlsplit(spec.url)
            if spec_parts.netloc and spec_parts.netloc != site_parts.netloc:
                spec.url = urlunsplit((
                    site_parts.scheme or spec_parts.scheme,
                    site_parts.netloc,
                    spec_parts.path,
                    spec_parts.query,
                    spec_parts.fragment,
                ))

        # sysmon (best-effort)
        sysmon_session = None
        if site is not None:
            try:
                from ..probes import sysmon
                sysmon_session = sysmon.start(site, duration_cap_s=max(5, max_requests * 0.1),
                                              label=f"{platform}:{operation}")
            except Exception:
                sysmon_session = None

        result: LoadResult = asyncio.run(
            run_load_test(spec, bench_secret=bench_secret,
                          dump_sample_every=0,
                          on_result=None, progress=None)
        )
        row.update(result.as_row())

        if sysmon_session is not None:
            try:
                row["server"] = sysmon_session.stop_and_collect()
            except Exception as exc:
                row["server_error"] = str(exc)

        row["status"] = "ok"
        return row

    # ------------------------------------------------------------------
    def run_all(self, resume: bool = True) -> dict:
        import signal

        cells = self.plan_cells()
        done = self.store.completed_keys() if resume else set()
        planned = len(cells)
        completed = failed = skipped = 0
        started_at = _now_iso()
        run_started = time.time()
        cell_durations: list[float] = []
        stop_requested = {"flag": False, "signal": None}

        def _request_stop(signum, _frame):
            stop_requested["flag"] = True
            stop_requested["signal"] = signum

        # SIGINT (Ctrl+C) and SIGTERM (bench pause / kill) both request a
        # graceful stop: finish the current cell, then exit. This preserves
        # data integrity — mid-cell aborts would leave a hole in results.jsonl.
        try:
            signal.signal(signal.SIGINT, _request_stop)
            signal.signal(signal.SIGTERM, _request_stop)
        except (ValueError, OSError):
            # signal handlers only work on the main thread; degrade gracefully
            pass

        # Initial heartbeat: idle, but planned count is now known
        self._write_heartbeat({
            "pid": os.getpid(),
            "state": "starting",
            "started_at": started_at,
            "total_cells": planned,
            "already_done": len(done),
            "current": None,
        })

        for i, cell in enumerate(cells, 1):
            key = self.store.cell_key(cell)
            if key in done:
                skipped += 1
                continue

            cell_started = time.time()
            self._write_heartbeat({
                "pid": os.getpid(),
                "state": "running",
                "started_at": started_at,
                "total_cells": planned,
                "already_done": len(done),
                "completed_this_run": completed,
                "failed_this_run": failed,
                "cells_seen": i,
                "current": {
                    "index": i,
                    "cell": cell,
                    "started_at": _now_iso(),
                },
                "avg_cell_seconds": (sum(cell_durations) / len(cell_durations)) if cell_durations else None,
                "elapsed_seconds": round(time.time() - run_started, 1),
            })

            try:
                row = self.run_cell(cell)
                self.store.append(row)
                if row.get("status") == "ok":
                    completed += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                self.store.append({**cell, "status": "failed", "error": str(exc)})

            cell_durations.append(time.time() - cell_started)
            if self.heartbeat:
                self.heartbeat(i, planned, cell)

            if stop_requested["flag"]:
                self._write_heartbeat({
                    "pid": os.getpid(),
                    "state": "paused",
                    "started_at": started_at,
                    "paused_at": _now_iso(),
                    "total_cells": planned,
                    "already_done": len(done),
                    "completed_this_run": completed,
                    "failed_this_run": failed,
                    "current": None,
                    "elapsed_seconds": round(time.time() - run_started, 1),
                    "stop_signal": stop_requested["signal"],
                })
                return {"planned": planned, "completed": completed,
                        "failed": failed, "skipped_or_resumed": skipped,
                        "paused": True}

        self._write_heartbeat({
            "pid": os.getpid(),
            "state": "finished",
            "started_at": started_at,
            "finished_at": _now_iso(),
            "total_cells": planned,
            "already_done": len(done),
            "completed_this_run": completed,
            "failed_this_run": failed,
            "current": None,
            "elapsed_seconds": round(time.time() - run_started, 1),
        })

        return {"planned": planned, "completed": completed,
                "failed": failed, "skipped_or_resumed": skipped}
