"""
Report logger — Python port of `utils/Logger.js` (parity P18–P24).

In-memory buffer written once at the end, summary prepended, per-request status
classification (200/403/404), body dumps, and flush-on-signal.
"""
from __future__ import annotations

import atexit
import json
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..core.metrics import LoadResult


def timestamp_slug() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat().replace(":", "-").replace(".", "-")


class ReportLogger:
    def __init__(self, directory: str | Path, file_prefix: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        safe_prefix = file_prefix.replace("/", "_").replace("-", "_")
        self.file_name = f"{safe_prefix}{timestamp_slug()}.log"
        self.path = self.directory / self.file_name
        self._buf: str = ""
        self._written = False
        self._install_signal_handlers()

    # ---- buffer ----------------------------------------------------------
    def log(self, message: str, on_start: bool = False) -> None:
        if on_start:
            self._buf = message + self._buf
        else:
            self._buf += message + "\n"

    def maybe_serialize(self, data: Any, on_start: bool = False) -> None:
        if data is None:
            self.log("Unable To Connect Server", on_start)
            return
        if isinstance(data, (dict, list)):
            self.log(json.dumps(data, indent=2), on_start)
            return
        try:
            parsed = json.loads(data)
            self.log(json.dumps(parsed, indent=2), on_start)
        except (json.JSONDecodeError, TypeError):
            self.log(str(data), on_start)

    # ---- per-request line (parity P18/P19/P20) ---------------------------
    def log_request(self, index: int, status: Optional[int], elapsed_ms: float,
                    body: str, dump_body: bool) -> None:
        self.log(
            f"Request Number: {index} ---- Status: {status} "
            f"---- Time in milliseconds: {round(elapsed_ms)}"
        )
        log_body = True
        if status == 200:
            self.log("Success\n")
            log_body = False
        elif status == 403:
            self.log("Validation Error: ")
        elif status == 404:
            self.log("Route Not Found: ")
        if log_body or dump_body:
            self.maybe_serialize(body)

    # ---- summary (parity P21/P22) ----------------------------------------
    def log_summary(self, result: LoadResult) -> None:
        if result is None or result.total_requests == 0:
            return
        p = result.percentiles()
        info = (
            "************************\n"
            f"URL : {result.url}\n"
            f"Total request : {result.total_requests}\n"
            f"Total Time in sec : {round(result.total_time_seconds, 3)}\n"
            f"Average Time per request in ms : {result.average_ms:.2f}\n"
            f"Success: {result.total_successes}\n"
            f"Errors: {result.total_errors}\n"
        )
        if result.error_codes:
            info += "\n\nError Status: \n" + json.dumps(result.error_codes, indent=2)
        info += "\n\nPercentiles : \n" + json.dumps(p, indent=2)
        # extended profiler means (new capability, appended, non-breaking)
        means = result.profiler_means()
        if means:
            info += "\n\nProfiler (means) : \n" + json.dumps(means, indent=2)
        info += "\n************************\n\n"
        self.log(info, on_start=True)

    # ---- flush (parity P23/P24) ------------------------------------------
    def write(self) -> None:
        if self._written:
            return
        self._written = True
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(self._buf)

    def _install_signal_handlers(self) -> None:
        atexit.register(self.write)

        def _handler(signum, frame):
            self.write()
            raise SystemExit(1)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # not in main thread; atexit still covers normal exit
                pass
