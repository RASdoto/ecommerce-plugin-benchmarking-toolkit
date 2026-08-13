"""
Async HTTP load engine — Python port of the Node `loadtest` execution model
(parity P9–P12). Sends exactly `max_requests` requests with at most
`concurrency` in flight, times each, reads profiler headers when present, and
invokes a per-request callback.

This replaces the `loadtest` npm package entirely.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Awaitable, Callable, Optional

import aiohttp

from .metrics import LoadResult
from .module_base import RequestSpec
from ..probes.headers import bench_request_headers, parse_profiler_headers

# statusCallback(index:int, status:int|None, elapsed_ms:float, body:str, profiler:dict)
StatusCallback = Callable[[int, Optional[int], float, str, dict], None]


async def run_load_test(
    spec: RequestSpec,
    *,
    bench_secret: str = "",
    dump_sample_every: int = 0,
    request_timeout: float = 30.0,
    on_result: Optional[StatusCallback] = None,
    progress: Optional[Callable[[], None]] = None,
) -> LoadResult:
    """Execute the load test described by `spec`.

    `bench_secret` (when set) enables the profiler headers on every request.
    `dump_sample_every` > 0 asks the profiler to dump the query list on every
    Nth request (for EXPLAIN/N+1 analysis).
    """
    result = LoadResult(url=spec.url)
    sem = asyncio.Semaphore(max(1, spec.concurrency))

    ssl_ctx: object
    if spec.insecure:
        ssl_ctx = False  # aiohttp: disable verification
    else:
        ssl_ctx = None

    connector = aiohttp.TCPConnector(limit=max(1, spec.concurrency), ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=request_timeout)

    base_headers = dict(spec.headers)

    async def one(index: int) -> None:
        # per-request body (deep-copied + mutated) then serialized (parity P9)
        body_obj = spec.module.render_body(index)
        payload = json.dumps(body_obj)

        headers = dict(base_headers)
        if bench_secret:
            dump = bool(dump_sample_every) and (index % dump_sample_every == 0)
            headers.update(bench_request_headers(bench_secret, dump=dump))
        # Only advertise Content-Length when we're actually sending a body.
        # Setting it on a bodyless GET makes servers wait for phantom bytes
        # and stall until the client times out.
        if spec.method != "GET":
            headers["Content-Length"] = str(len(payload.encode("utf-8")))

        status: Optional[int] = None
        text = ""
        profiler: dict = {}
        t0 = time.perf_counter()
        async with sem:
            try:
                send_body = None if spec.method == "GET" else payload
                async with session.request(
                    spec.method, spec.url, data=send_body, headers=headers
                ) as resp:
                    status = resp.status
                    text = await resp.text()
                    profiler = parse_profiler_headers(resp.headers)
            except Exception as exc:  # timeout, conn reset, dns, etc.
                text = f"{type(exc).__name__}: {exc}"
                status = None
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # record
        result.timings_ms.append(elapsed_ms)
        # Treat any 2xx as success. POST/PUT commonly return 201/202/204;
        # counting those as errors mis-classifies successful writes.
        if status is None or not (200 <= status < 300):
            result.total_errors += 1
            code = str(status) if status is not None else "ERROR"
            result.error_codes[code] = result.error_codes.get(code, 0) + 1
        for k, v in profiler.items():
            result.profiler.setdefault(k, []).append(v)

        if on_result is not None:
            on_result(index, status, elapsed_ms, text, profiler)
        if progress is not None:
            progress()

    run_start = time.perf_counter()
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        await asyncio.gather(*(one(i + 1) for i in range(spec.max_requests)))
    result.total_time_seconds = time.perf_counter() - run_start
    result.total_requests = len(result.timings_ms)
    return result
