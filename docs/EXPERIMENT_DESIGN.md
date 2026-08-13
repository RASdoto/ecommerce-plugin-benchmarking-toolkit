# Experiment design — the test matrix

The toolkit runs a **full factorial** of `platform × operation × volume ×
concurrency`. All knobs live in [`matrix.yaml`](../matrix.yaml).

## Factors and levels (defaults)

| Factor | Levels |
|---|---|
| **Platform** | `fluentcart`, `woo`, `edd` |
| **Operation** | `product-list`, `product-create`, `order-create` (extensible) |
| **Product volumes** | 1,000 · 10,000 · 50,000 · 100,000 |
| **Order / customer volumes** | 100 · 500 · 1,000 · 2,500 · 5,000 |
| **Read concurrency** | 1 · 10 · 25 · 50 · 100 |
| **Write concurrency** | 1 · 5 · 10 · 25 · 50 |

Volumes are per-entity and **hard-capped** (products ≤ 100k; orders / customers ≤
5k) so a smoke run never has to build an unbounded catalogue.

## Operation × axis applicability

| Operation | Sweeps volume as… | Sweeps concurrency | Prerequisites |
|---|---|---|---|
| `product-list` (read) | **pre-existing catalogue size** (a true data-scaling axis) | read tiers | products exist |
| `product-create` (write) | request-burst cap `min(volume, conc×20)` | write tiers | none |
| `order-create` (write) | request-burst cap `min(volume, conc×20)` | write tiers | products **and** customers exist (verified, not created) |

Because reads grow the catalogue monotonically, the toolkit measures each read
tier in isolation by **resetting to zero and growing ascending** (0 → 1k → 10k →
50k → 100k), so a `v10000` cell is measured with exactly 10,000 rows present.

## Seed → measure ordering

1. **Ensure** the cell's target volume / prerequisites exist (idempotent; see
   [SEEDING.md](SEEDING.md)).
2. **Start** the host system monitor.
3. **Run** the HTTP load with profiler headers.
4. **Stop** the monitor, collect DB/CPU/RAM, append one result row.

Order-create prerequisites are **verified, never auto-created** — creating them at
measure time would corrupt the volume counts other cells rely on. A cell with
missing prerequisites is skipped and recorded as such.

## What the charts are for

The chart set (see [PUBLISHING.md](PUBLISHING.md)) is organised around neutral,
falsifiable questions — each figure exists to answer one, whichever way the data
falls:

- **Scalability** — does read latency / query count stay flat as the catalogue
  grows, or climb?
- **Concurrency** — how high can each platform push concurrency before errors or
  throughput collapse?
- **Efficiency** — CPU-seconds, queries, and memory *per operation*.
- **Write cost** — how much database work does creating a product or an order
  take on each platform?

Results are reported as the data gives them, including outcomes unfavourable to
any platform. Single-run cells carry run-to-run noise; for publication-grade
numbers, raise the repeat count or average multiple runs.
