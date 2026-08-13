# eCommerce Plugin Benchmarking Toolkit

A reproducible load-testing harness for **WordPress e-commerce plugins** —
**FluentCart**, **WooCommerce**, and **Easy Digital Downloads (EDD)** — measured
on equivalent hardware under an identical, factorial test matrix.

It answers one question fairly: **as a store accumulates products, customers, and
orders, how does each plugin's data model hold up under load?** It measures
latency, throughput, per-request database work, PHP memory, and server CPU/RAM —
then turns the results into charts and (optionally) a written report.

> **Vendor-neutral by design.** The toolkit measures whatever the data shows and
> reports it honestly, including results unfavourable to any one platform. Nothing
> is hard-coded to make a particular plugin "win."

---

## Table of contents

- [What it produces](#what-it-produces)
- [How it works (the pipeline)](#how-it-works-the-pipeline)
- [Requirements](#requirements)
- [Setup](#setup)
  - [1. The credentials file — the only thing you edit](#1-the-credentials-file--the-only-thing-you-edit)
  - [2. Install dependencies](#2-install-dependencies)
- [Running a benchmark](#running-a-benchmark)
- [How dependencies work](#how-dependencies-work)
- [How each platform is seeded (important)](#how-each-platform-is-seeded-important)
- [Command reference](#command-reference)
- [Security & what never gets committed](#security--what-never-gets-committed)
- [Documentation map](#documentation-map)

---

## What it produces

Each run writes to `reports/` (git-ignored):

- `reports/dataset.csv` — one row per matrix cell, every metric column (chartable).
- `reports/results.jsonl` — the append-only raw store (resumable, crash-safe).
- `reports/run_manifest.json` — the completion report: cells planned/completed/
  failed, per-cell timing, and the environment snapshot (PHP/MySQL/WP/cores).
- `reports/summary.json` — headline counts.

The optional publishing stage (`bench publish`) turns that into charts, a
scientific report, and comparison pages under `Publish/` (also git-ignored). See
[docs/PUBLISHING.md](docs/PUBLISHING.md).

---

## How it works (the pipeline)

```
credentials.yaml ─▶ bootstrap ─▶ preflight ─▶ seed ─▶ run-all ─▶ report ─▶ (publish)
                     provision     validate    fill    measure    aggregate   charts
```

1. **Bootstrap** — per site, over SSH + REST, idempotently: installs **WP-CLI** if
   missing, deploys the **`bench-probe` profiler** (as an mu-plugin, or via REST
   plugin upload as a fallback), **mints a WP Application Password** if only a
   login password was given, captures an admin session, and snapshots the
   environment. Verifies the profiler responds to an `X-Bench` request.
2. **Preflight** — fail-fast validation: config valid, every site reachable,
   profiler responding, credentials present, DB reachable. Stops with a precise
   reason if anything is missing.
3. **Seed** — brings each site to the volumes the matrix needs, using each
   platform's **native creation path** (see [seeding](#how-each-platform-is-seeded-important)).
   Idempotent and rate-limited.
4. **Run-all** — executes `platform × operation × volume × concurrency`. Per cell:
   ensure volume → start the system monitor → run the HTTP load (with profiler
   headers) → stop the monitor → append one row. **Resumable**: re-running skips
   completed cells.
5. **Report** — aggregates the store into `dataset.csv`, `summary.json`, and
   `run_manifest.json`.
6. **Publish** (optional) — computes findings, renders charts, and generates a
   report + comparison pages.

The knobs (volumes, concurrency tiers, operations, rate limits) live in
[`matrix.yaml`](matrix.yaml) and ship with capped, realistic defaults.

---

## Requirements

**On the machine running the toolkit (the controller):**

- Python 3.10+
- The Python packages in [`requirements.txt`](requirements.txt) (see
  [dependencies](#how-dependencies-work))
- Network reach (HTTP + SSH) to the benchmark sites

**For each site under test (provide 3 for a full comparison):**

- A **dedicated, non-production** WordPress install with the plugin active
  (FluentCart, WooCommerce, or EDD), on **equivalent hardware/PHP/MySQL** for a
  fair comparison.
- **SSH access** with WP-CLI available or installable (the toolkit installs it if
  missing). SSH is required to seed orderable products.
- WordPress **admin credentials** (or a WP Application Password).

> ⚠️ **Runs are destructive to data.** Seeding inserts and bulk actions run
> against the target sites. Only ever point the toolkit at dedicated benchmark
> installs you control.

---

## Setup

### 1. The credentials file — the only thing you edit

**This is the single manual step.** Copy the template and fill one block per site:

```bash
cp credentials.example.yaml credentials.yaml
```

Then edit `credentials.yaml`. Each site block needs a URL, admin credentials, and
SSH access:

```yaml
fluentcart:
  url:        "https://your-fluentcart-site.example.com"   # site base URL      (required)
  admin_user: "admin"                                      # WP admin username  (required)
  admin_pass: "your-admin-password"                        # admin password …   (required)
  app_password: ""                                         # … OR a WP App Password; blank ⇒ auto-minted
  ssh_host:   "your-fluentcart-site.example.com"           # SSH host           (required)
  ssh_port:   22
  ssh_user:   "sshuser"                                    #                    (required)
  ssh_key:    "~/.ssh/id_rsa"                              # key path …         (required: key OR pass)
  ssh_pass:   ""                                           # … OR SSH password
  api:                                                     # optional REST keys some modules use
    # WOO_CONSUMER_KEY: "ck_..."
    # WOO_CONSUMER_SECRET: "cs_..."
woo:   { … same fields … }
edd:   { … same fields … }
```

**`credentials.yaml` is git-ignored and must never be committed.** It is the only
file that holds real secrets. Everything else the toolkit needs — WP-CLI, the
profiler plugin, an application password, the admin cookie — is auto-provisioned
by `bench bootstrap` and stored in the git-ignored `.secrets/` directory. See
[Security](#security--what-never-gets-committed).

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium          # only if you use the browser-metrics probe
```

---

## Running a benchmark

One command chains provisioning → validation → seeding → measurement → report:

```bash
python -m bench autorun
```

Or run the stages individually (useful for long runs and debugging):

```bash
python -m bench bootstrap     # provision WP-CLI + profiler + credentials
python -m bench preflight     # fail-fast validation
python -m bench seed --all    # bring sites to target volumes
python -m bench run-all       # measure the full matrix (resumable)
python -m bench report        # aggregate → reports/*.csv|json
```

Monitor a long run from another shell:

```bash
python -m bench progress      # live progress bar + ETA
python -m bench pause         # graceful stop (finishes current cell)
python -m bench resume        # continue where it left off
```

A run is **resumable and crash-safe**: each cell is written immediately, and
re-running `run-all`/`resume` skips completed cells.

---

## How dependencies work

The toolkit keeps its dependency surface deliberately small.

**Python packages** (`requirements.txt`):

| Package | Used for |
|---|---|
| `pyyaml` | reading `credentials.yaml` and `matrix.yaml` |
| `requests` | REST calls (bootstrap, seeding, ref resolution) |
| `aiohttp` | the async HTTP load engine (concurrent request bursts) |
| `paramiko` | SSH + SFTP to each site (WP-CLI, profiler deploy, DB counts) |
| `tqdm` | progress bars |
| `playwright` | *optional* browser-side metrics (LCP, admin render). Skip `playwright install` if you don't need them. |
| `matplotlib` | *optional* — only needed for the `publish` charting stage |

Install everything with `pip install -r requirements.txt` inside a virtualenv so
it never touches your system Python. `matplotlib` and `playwright` are only
exercised by optional stages; the core benchmark runs without them.

**Server-side dependencies are auto-provisioned** — you do **not** install them by
hand:

- **WP-CLI** — `bench bootstrap` checks `wp --info` and downloads `wp-cli.phar`
  if absent.
- **`bench-probe`** — the per-request profiler plugin, deployed as an mu-plugin
  over SSH/SFTP, or uploaded + activated over REST if the filesystem isn't
  writable. It only activates for requests carrying the correct secret header, so
  normal site traffic is never profiled or slowed.
- **Application password / admin session** — minted and captured automatically if
  only a login password was supplied.

---

## How each platform is seeded (important)

FluentCart, WooCommerce, and EDD store commerce data very differently, so each is
populated through **its own native creation path** — FluentCart via its models
and REST API (custom `fct_*` tables), WooCommerce via WC functions and
schema-accurate SQL (`wp_posts` + `wp_postmeta` + lookup tables), EDD via its
own functions (`download` posts + `edd_*` tables). What's held constant is the
*target state* and the *measurement*, not the insertion mechanism — so the
benchmark exercises each plugin's genuine data model.

This has real operational consequences (e.g. WooCommerce needs its product lookup
table regenerated after a bulk seed). **Read [docs/SEEDING.md](docs/SEEDING.md)
for the full per-platform breakdown** — it is essential for interpreting results
correctly.

---

## Command reference

| Command | What it does |
|---|---|
| `bench bootstrap` | Auto-provision WP-CLI, profiler, credentials per site |
| `bench preflight` | Fail-fast validation of config + connectivity |
| `bench seed --all` | Seed each site to the matrix volumes (idempotent) |
| `bench run-all` | Run the full matrix (resumable, crash-safe) |
| `bench report` | Aggregate results → `reports/dataset.csv` + manifests |
| `bench autorun` | bootstrap && preflight && seed && run-all && report |
| `bench progress` | Live progress snapshot of the current/last run |
| `bench pause` / `resume` | Graceful stop / continue |
| `bench teardown` | Optionally remove the profiler from all sites |
| `bench publish` | Findings + charts + report from `reports/` → `Publish/` |
| `bench compare --vs woo\|edd\|all` | Scoped head-to-head comparison bundles |

---

## Security & what never gets committed

The `.gitignore` in this repo blocks every secret- and artifact-bearing path:

- `credentials.yaml` — **your real URLs, passwords, SSH keys.** Never committed.
- `.secrets/` — agent-derived secrets: minted app passwords, admin cookies, the
  profiler secret, bootstrap state.
- `reports/` and `Publish/` — run outputs (may embed site hostnames).
- `*.key`, `*.pem`, `id_rsa*`, `.venv/`, `__pycache__/`, `*.zip`.

Only `credentials.example.yaml` (a placeholder template) is tracked. The toolkit
scrubs secrets from logs and never echoes them. **Before publishing this repo,
confirm `git status` shows no `credentials.yaml`, no `.secrets/`, and no
`reports/`/`Publish/`.**

---

## Documentation map

| Doc | Contents |
|---|---|
| [docs/AGENT_RUNBOOK.md](docs/AGENT_RUNBOOK.md) | Drop-in runbook: load into any coding agent to run the benchmark unattended |
| [docs/SEEDING.md](docs/SEEDING.md) | How products/customers/orders are created for each platform, and why the methods differ |
| [docs/METRICS.md](docs/METRICS.md) | What each metric measures and how it's collected |
| [docs/EXPERIMENT_DESIGN.md](docs/EXPERIMENT_DESIGN.md) | The factorial matrix, volume/concurrency tiers, and operation applicability |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | The chart + report generation pipeline |

---

## License

Add a license of your choice before publishing (e.g. `LICENSE` with MIT or
GPL-2.0-or-later to match the WordPress ecosystem).
