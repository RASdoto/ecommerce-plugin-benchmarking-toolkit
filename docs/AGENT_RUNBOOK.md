# Agent Runbook — run the benchmark unattended

**Audience:** an automation/coding agent (Claude Code, Copilot, Cursor, etc.) that
has been handed this repository and a filled `credentials.yaml`.
**Promise:** after a one-time credentials step, a single command provisions,
validates, seeds, measures, and reports the entire matrix. This file is
self-contained — load it into any agent and follow it top to bottom.

---

## 0. Preconditions the human provides

1. Three dedicated, **non-production** benchmark sites (FluentCart / WooCommerce /
   EDD) on **equivalent hardware/PHP/MySQL**, each with the plugin active.
2. A filled `credentials.yaml` (copied from `credentials.example.yaml`): per site,
   **URL + admin credentials + SSH access**. Nothing else.
3. The controller machine has Python 3.10+ and network reach (HTTP + SSH) to all
   three sites.

The agent does **everything else** — do not hand-install WP-CLI, copy plugins,
create app passwords, or capture cookies.

---

## 1. One-time controller setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional, only for browser-side metrics:
playwright install chromium
```

Optionally review `matrix.yaml` (volumes / concurrency / operations). It ships
with capped, realistic defaults (products ≤ 100k, orders ≤ 5k) and needs no edits.

---

## 2. The unattended flow

The intended entry point:

```bash
python -m bench autorun
```

`autorun` = `bootstrap && preflight && seed --all && run-all && report`. It exits
`0` on complete, `2` on complete-with-gaps, `1` on blocked, and always writes
`reports/run_manifest.json`.

If you prefer to drive the stages yourself:

| Step | Command | What it must accomplish before proceeding |
|---|---|---|
| B0 | `bench bootstrap` | WP-CLI present, `bench-probe` responding with headers, app password + admin cookie in `.secrets/`, environment snapshot captured. Re-runnable; skips done work. |
| B1 | `bench preflight` | Every site reachable, profiler verified, DB reachable. **Do not proceed if this fails** — report the failing check. |
| B2 | `bench seed --all` | Baseline footprint captured; sites seeded toward matrix volumes (also auto-seeded per cell). |
| B3 | `bench run-all` | Executes `platform × volume × concurrency × operation`. Resumable — re-running skips completed cells. |
| B4 | `bench report` | Writes `reports/dataset.csv`, `summary.json`, `run_manifest.json`. |

---

## 3. Platform-specific gotcha the agent must handle

**WooCommerce product lookup table.** WooCommerce products are seeded with fast
bulk SQL, which does **not** populate `wc_product_meta_lookup`. Before measuring
WooCommerce, regenerate it and let the queue drain, or WooCommerce runs a
background regeneration *during* the run and inflates its write numbers:

```bash
wp wc tool run regenerate_product_lookup_tables --user=1   # over SSH on the Woo site
wp action-scheduler run                                    # drain pending jobs
```

The agent should verify a plain `GET /wp-json/wc/v3/products` returns quickly and
that Action Scheduler has no *due* pending actions before trusting Woo write
cells. Full rationale in [SEEDING.md](SEEDING.md).

---

## 4. Decisions the agent makes on its own

| Situation | Action |
|---|---|
| WP-CLI missing on a server | Bootstrap installs it over SSH. No action needed. |
| SSH filesystem write blocked | Bootstrap falls back to REST plugin upload of `bench-probe`; records which path was used. |
| Only a login password given | Bootstrap mints an Application Password automatically. |
| A single cell errors mid-matrix | Log it, mark the cell failed, continue. It appears in the manifest. |
| A whole site unreachable | Skip that platform's remaining cells, record the fallback, continue. |
| Interrupted / killed | On restart, `bench run-all` (or `resume`) finishes only unfinished cells. |
| Seeding can't reach a volume | Cap at the highest reached level and record the cap — no silent truncation. |
| Order-create prerequisites missing | The cell is **skipped** (prereqs are verified, never auto-created). Seed products + customers first, then re-run. |
| Credentials simply wrong | Stop and report which site + field. Do not loop. |

The agent never retries a hard-failed preflight in a loop, never fabricates a
missing metric, and never points seeding at a URL absent from `credentials.yaml`.

---

## 5. Completion signal

The agent's final action is to surface `reports/run_manifest.json` and a one-line
status:

- **complete** — all planned cells succeeded.
- **complete-with-gaps** — finished, but some cells failed or fell back (the
  manifest lists exactly which and why).
- **blocked** — preflight or a hard dependency failed before meaningful work; the
  manifest names the blocker.

Report the manifest path and the status line, and nothing more unless asked.

---

## 6. Optional: turn results into charts and a report

```bash
python -m bench publish                    # findings + charts + report → Publish/
python -m bench compare --vs all           # scoped head-to-head bundles
```

Publishing reads numbers only from the computed findings file (no fabricated
values), reports every verdict honestly (including unfavourable ones), and scrubs
secrets from all output. See [PUBLISHING.md](PUBLISHING.md).

---

## 7. Safety

- Runs are **destructive** to data on target sites (seeding, bulk actions). Only
  point the toolkit at dedicated benchmark installs.
- Real secrets live only in `credentials.yaml`; agent-derived secrets live in
  `.secrets/`. Both are git-ignored. Never write either into logs, reports, or any
  published artifact.
- The profiler only activates on requests carrying the correct secret header, so
  normal traffic is never profiled. `bench teardown` removes it when done.
