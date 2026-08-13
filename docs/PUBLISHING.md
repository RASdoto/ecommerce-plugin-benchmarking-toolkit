# Publishing — charts, reports, and comparisons

After `bench report` has written `reports/dataset.csv`, the publishing stage turns
the results into shareable deliverables. It is optional and requires `matplotlib`
(already in `requirements.txt`).

## Commands

```bash
python -m bench publish                 # full 3-way bundle → Publish/
python -m bench compare --vs all        # scoped head-to-heads → Publish/vs-woo, Publish/vs-edd
python -m bench compare --vs woo         # just one comparison
```

Individual stages (all idempotent, each overwrites its own output):

```bash
python -m bench derive     # reports/ → Publish/findings.json + derived.csv
python -m bench plots      # findings → Publish/plots/*.{png,svg,json}
python -m bench article    # findings + plots → Publish/article/*.md|html
python -m bench landing    # findings + plots → Publish/landing/index.html
```

## What each stage produces (under `Publish/`, git-ignored)

| Path | Contents |
|---|---|
| `findings.json` | **The single source of truth.** Every derived number and each verdict, computed from the results store. |
| `derived.csv` | Long-form table of derived metrics for spot-checking. |
| `plots/<ID>.{png,dark.png,svg,json,caption.json}` | Each figure as raster (light + dark), vector, raw series data, and a caption sidecar. |
| `article/benchmark-report.md` (+ `.html`) | A scientific-style report: method, results per question, honest limitations. |
| `landing/index.html` (+ `blocks.html`) | A standalone summary page (and a WordPress-block variant). |
| `publish_manifest.json` | What was generated, which figures were skipped and why, and each verdict. |
| `vs-woo/`, `vs-edd/` | Scoped head-to-head bundles (`bench compare`) with their own findings, charts, article, and landing. |

## Anti-fabrication guardrails (built in)

1. **No fabricated numbers.** All prose and every chart caption resolve to a field
   in `findings.json` / `derived.csv`. A missing number is reported as missing,
   never invented.
2. **Honest verdicts.** Each hypothesis/comparison verdict is decided from the
   data — `supported`, `inconclusive`, or `refuted` — and reported plainly,
   including outcomes unfavourable to any platform. A head-to-head "posture"
   (dominant vs. scoped) is *derived from win-rates*, not asserted.
3. **Disclosed exclusions.** Skipped figures and excluded cells are counted in the
   manifest and footnoted on affected charts. Outliers are never silently
   smoothed; where a bar is clamped for readability, its true value is labelled.
4. **No secrets in output.** Site hostnames, credentials, and `.secrets/` contents
   never appear in any deliverable. Default labels are "FluentCart / WooCommerce /
   EDD site on identical hardware."
5. **Relative latency framing.** Because p95 is closed-loop (see
   [METRICS.md](METRICS.md)), latency is presented as *relative / throughput* with
   the raw milliseconds footnoted as not-a-page-load-time.

## Head-to-head comparisons

`bench compare` builds one bundle per competitor. For each `(operation, metric)`
it computes FluentCart's win-rate and median advantage across the matrix, assigns
a per-metric verdict (`fc_wins` / `tie` / `fc_loses`), and renders a diverging
**scorecard** chart that shows wins *and* losses on the same axis. The article and
landing lead with genuine strengths and disclose where the competitor leads — that
disclosure is what makes the strengths credible.
