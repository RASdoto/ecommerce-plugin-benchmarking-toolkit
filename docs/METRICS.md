# Metrics — what is measured and how

Every matrix cell produces one row of metrics. This document defines each metric,
how it is collected, and one methodological caveat you must understand to read the
latency numbers correctly.

## Collection tiers

| Tag | Source | How |
|---|---|---|
| **[LT]** | Load-test client | The async HTTP engine ([`bench/core/httpload.py`](../bench/core/httpload.py)) times every request and records status codes. |
| **[LT+P]** | Load test + in-request profiler | The `bench-probe` mu-plugin returns per-request server-side metrics in response headers, but only for requests carrying the correct secret. |
| **[SSH]** | Host system monitor | A sampler records CPU% and memory on the target host during each cell. |
| **[CLI]** | WP-CLI over SSH | Row counts, install footprint, DB size. |
| **[BR]** | Headless browser (optional) | Rendered page metrics (LCP, JS weight) via Playwright. |

## Metric definitions

| Metric | Tier | Meaning |
|---|---|---|
| `throughput_rps` | [LT] | Requests completed per second (wall-clock): `total_requests / total_time`. |
| `p50/p90/p95/p99_ms` | [LT] | Client-observed latency percentiles (see caveat below). |
| `avg_ms` | [LT] | `total_time / total_requests × 1000` — the reciprocal of throughput; at concurrency 1 it equals the mean per-request wall time. |
| `error_rate_pct` | [LT] | Share of non-2xx / failed requests. |
| `error_codes` | [LT] | Breakdown of failures by HTTP status (or `ERROR` for timeouts/resets). |
| `mean_server_ms` | [LT+P] | In-PHP request processing time (excludes network / queueing). |
| `mean_query_count` | [LT+P] | SQL queries issued per request. |
| `mean_query_time_ms` | [LT+P] | Total in-request database time per request. |
| `mean_slow_queries` | [LT+P] | Slow queries per request. |
| `mean_peak_mem_mb` | [LT+P] | PHP peak memory per request. |
| `mean_php_time_ms` | [LT+P] | PHP execution time per request. |
| host CPU / RAM | [SSH] | Sampled CPU% (user/system) and memory during the cell. |

## ⚠️ The latency caveat (read this)

Latency is measured **closed-loop**: the load engine creates all N requests at
once and admits at most `concurrency` at a time via a semaphore, timing each
request from **creation** — which includes the time it waits for a concurrency
slot. Consequences:

- **`p50/p90/p95/p99` are queue-inclusive.** At concurrency 1 the N requests run
  strictly in sequence, so the reported p95 is roughly `(N − 0.5) × service_time`
  — about **9.5× the true per-request time** for the default 10-sample read cells.
  These are *not* real page-load times.
- **The inflation multiplier is uniform across platforms** (same N, same
  concurrency in a given cell), so it **cancels in relative comparison**:
  platform *rankings* and *percentage margins* between platforms are exact, even
  though the absolute millisecond values are inflated.
- **The un-inflated signals** are `mean_server_ms` (actual in-PHP time) and
  `throughput_rps`. Use those for absolute claims; use p95 only for relative
  comparison, and never present a raw p95 figure as a page-load latency.

This is why the publishing stage frames latency as *relative* / *throughput* and
footnotes the closed-loop nature.

## A note on write-operation "volume"

For read operations, `volume` is the number of rows present when the read is
measured — a genuine data-scaling axis. For **write** operations
(`product-create`, `order-create`), the request budget is
`min(volume, concurrency × 20)` — a **burst-size cap**, not a pre-existing-count
axis. At most concurrencies `concurrency × 20` binds, so the write matrices are
effectively concurrency sweeps against a fully-populated store, not
volume-scaling studies. Interpret them accordingly.
