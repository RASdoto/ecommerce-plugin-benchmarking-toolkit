"""
bench — command-line entry point.

Commands:
  bootstrap   auto-provision all sites (WP-CLI, profiler, app pw, admin cookie)
  preflight   fail-fast validation of config + connectivity + provisioning
  seed        capture install footprint + seed volumes (rate-limited)
  run         run a single config.json scenario (legacy parity path, like index.js)
  run-all     execute the full matrix (resumable)
  report      aggregate results -> dataset.csv + summary.json + run_manifest.json
  autorun     bootstrap && preflight && seed && run-all && report
  teardown    remove the profiler from all sites

Exit codes for autorun: 0 complete, 2 complete-with-gaps, 1 blocked.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .core.config import load_app_config
from .settings import Settings


def _settings(args) -> Settings:
    return Settings(root=args.root)


def _probe_path(root: str) -> str:
    return str(Path(root) / "bench-probe" / "bench-probe.php")


def _edd_api_path(root: str) -> str:
    return str(Path(root) / "bench-edd-api" / "bench-edd-api.php")


# --------------------------------------------------------------------------- cmds
def cmd_bootstrap(args) -> int:
    from .provision.bootstrap import Bootstrapper
    s = _settings(args)
    bs = Bootstrapper(s, _probe_path(args.root), _edd_api_path(args.root))
    if args.site:
        out = {args.site: bs.provision_site(args.site)}
        (s.secrets_dir / "bootstrap_state.json").write_text(json.dumps(out, indent=2, default=str))
    else:
        out = bs.provision_all()
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_preflight(args) -> int:
    from .preflight import check
    s = _settings(args)
    rep = check(s)
    print(json.dumps(rep, indent=2, default=str))
    return 0 if rep["ok"] else 1


def cmd_run(args) -> int:
    """Single scenario — mirrors the Node index.js flow (parity path)."""
    from .core.httpload import run_load_test
    from .modules.registry import make_module
    from .report.logger import ReportLogger
    from tqdm import tqdm

    s = _settings(args)
    app_config = load_app_config(s.config_json_path)
    module_path = args.module or app_config.get("default_module", "wp")
    platform = s.platform_for_module(module_path)

    # SEED -> MEASURE ordering: for order placement, ensure prerequisite products
    # + customers exist first (same rule for FC/Woo/EDD), THEN resolve refs.
    if module_path == "fluent-cart/order/create" and s.sites:
        from .seed.factory import prepare_platform_data
        from .seed.fluentcart import ensure_fc_order_refs
        print("[seed] ensuring prerequisite products + customers ...")
        prep = prepare_platform_data(s, "fluentcart", "order-create", 0)
        if not prep.get("seeded"):
            print(f"[seed][warn] prerequisite seeding issue: {prep.get('error') or prep.get('reason')}")
        info = ensure_fc_order_refs(s, "fluentcart", create_if_missing=False)
        if not info.get("ready"):
            print(f"[warn] FluentCart order refs not ready: {info.get('reason')}")
        else:
            print(f"[ok] resolved FC order refs: {info['refs']}")

    secret = s.secret_resolver(platform) if s.sites else (lambda k, d=None: d)
    module = make_module(module_path, app_config, s.module_dir, secret=secret)
    spec = module.to_spec()

    logger = ReportLogger(s.reports_dir, module_path.replace("/", "_") + "_")
    bar = tqdm(total=spec.max_requests, disable=spec.quiet)
    bench_secret = s.bench_secret(platform) if s.sites else ""

    def on_result(index, status, elapsed, body, profiler):
        logger.log_request(index, status, elapsed, body, spec.dump_body)
        bar.update(1)

    result = asyncio.run(
        run_load_test(spec, bench_secret=bench_secret, on_result=on_result)
    )
    bar.close()
    logger.log_summary(result)
    logger.write()
    print(f"\nReport: {logger.path}")
    print(json.dumps(result.as_row(), indent=2, default=str))
    return 0


def cmd_seed(args) -> int:
    from .run.runner import Runner  # seeding is invoked per-cell; standalone warmup here
    s = _settings(args)
    print("Seeding is invoked automatically per matrix cell during run-all.")
    print("Install-footprint capture + explicit warm-up seeding run here when "
          "site DB access is configured.")
    return 0


def cmd_run_all(args) -> int:
    from .run.runner import Runner
    from tqdm import tqdm
    s = _settings(args)
    app_config = load_app_config(s.config_json_path)

    bar = {"b": None}

    def hb(i, total, cell):
        if bar["b"] is None:
            bar["b"] = tqdm(total=total)
        bar["b"].update(1)
        bar["b"].set_description(f"{cell['platform']}/{cell['operation']} v{cell['volume']} c{cell['concurrency']}")

    runner = Runner(settings=s, app_config=app_config, heartbeat=hb)
    stats = runner.run_all(resume=not args.no_resume)
    if bar["b"]:
        bar["b"].close()
    print(json.dumps(stats, indent=2))
    return 0


def cmd_report(args) -> int:
    from .report.reporter import Reporter
    s = _settings(args)
    bstate = {}
    bpath = s.secrets_dir / "bootstrap_state.json"
    if bpath.exists():
        try:
            bstate = json.loads(bpath.read_text())
        except json.JSONDecodeError:
            bstate = {}
    # recompute run stats from store
    from .run.store import ResultStore
    store = ResultStore(s.reports_dir / "results.jsonl")
    rows = list(store.read_all())
    run_stats = {
        "completed": sum(1 for r in rows if r.get("status") == "ok"),
        "failed": sum(1 for r in rows if r.get("status") == "failed"),
        "planned": len(rows),
    }
    rep = Reporter(s)
    out = rep.build_all(run_stats, bstate)
    print(json.dumps(out, indent=2))
    return 0


def cmd_teardown(args) -> int:
    from .sshutil import from_site
    s = _settings(args)
    for key, site in s.sites.items():
        derived = s.load_derived(key)
        wp_path = derived.get("wp_path", "")
        try:
            with from_site(site) as ssh:
                if wp_path:
                    ssh.run(f"rm -f {wp_path}/wp-content/mu-plugins/bench-probe.php")
                print(f"{key}: profiler removed")
        except Exception as exc:
            print(f"{key}: teardown skipped ({exc})")
    return 0


def cmd_progress(args) -> int:
    from .run.progress import snapshot, render
    s = _settings(args)
    snap = snapshot(s)
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(render(snap))
    return 0


def cmd_pause(args) -> int:
    """Graceful pause: SIGTERMs the run-all process (finishes current cell)."""
    from .run.progress import pause, snapshot, render
    s = _settings(args)
    result = pause(s)
    print(json.dumps(result, indent=2, default=str))
    if result.get("ok"):
        print("\n" + render(snapshot(s)))
        return 0
    return 1


def cmd_resume(args) -> int:
    """Resume: same as `run-all` (which auto-skips completed cells)."""
    # Alias — the runner is inherently resume-safe via ResultStore.completed_keys()
    return cmd_run_all(args)


def cmd_autorun(args) -> int:
    print("== bootstrap =="); cmd_bootstrap(args)
    print("== preflight =="); pf = cmd_preflight(args)
    if pf != 0:
        print("Preflight failed — stopping (blocked).")
        return 1
    print("== seed =="); cmd_seed(args)
    print("== run-all =="); cmd_run_all(args)
    print("== report =="); cmd_report(args)
    # derive exit code from manifest
    s = _settings(args)
    manifest = s.reports_dir / "run_manifest.json"
    if manifest.exists():
        overall = json.loads(manifest.read_text()).get("overall")
        return {"complete": 0, "complete-with-gaps": 2, "blocked": 1}.get(overall, 2)
    return 2


def cmd_derive(args) -> int:
    from .publish import derive
    return derive.run()


def cmd_plots(args) -> int:
    from .publish import plots
    return plots.run()


def cmd_article(args) -> int:
    from .publish import article
    return article.run()


def cmd_landing(args) -> int:
    from .publish import landing
    return landing.run()


def cmd_publish(args) -> int:
    from .publish import publish
    return publish.run()


def cmd_compare(args) -> int:
    from .publish import pairwise
    targets = ["woo", "edd"] if args.vs in (None, "all") else [args.vs]
    rc = 0
    for t in targets:
        rc = pairwise.run(t) or rc
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description="FluentCart/Woo/EDD benchmark suite")
    p.add_argument("--root", default=".", help="project root (contains config.json, credentials.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap"); b.add_argument("--site", default=None); b.set_defaults(func=cmd_bootstrap)
    sub.add_parser("preflight").set_defaults(func=cmd_preflight)

    r = sub.add_parser("run"); r.add_argument("--module", default=None); r.set_defaults(func=cmd_run)
    sub.add_parser("seed").set_defaults(func=cmd_seed)

    ra = sub.add_parser("run-all"); ra.add_argument("--no-resume", action="store_true"); ra.set_defaults(func=cmd_run_all)
    sub.add_parser("report").set_defaults(func=cmd_report)
    sub.add_parser("teardown").set_defaults(func=cmd_teardown)

    pg = sub.add_parser("progress", help="show snapshot of the current/last run")
    pg.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    pg.set_defaults(func=cmd_progress)

    sub.add_parser("pause", help="graceful stop: SIGTERM the running run-all "
                                 "(finishes current cell, safe to resume)"
                   ).set_defaults(func=cmd_pause)

    rs = sub.add_parser("resume", help="continue a previously started run-all "
                                       "(skips cells already in results.jsonl)")
    rs.add_argument("--no-resume", action="store_true",
                    help="hidden: pass through to run-all for parity")
    rs.set_defaults(func=cmd_resume)

    au = sub.add_parser("autorun"); au.add_argument("--site", default=None)
    au.add_argument("--no-resume", action="store_true"); au.set_defaults(func=cmd_autorun)

    # ---- publishing pipeline (BENCHMARK_PUBLISHING_RUNBOOK) --------------
    sub.add_parser("derive", help="reports/ -> Publish/findings.json + derived.csv").set_defaults(func=cmd_derive)
    sub.add_parser("plots", help="findings -> Publish/plots/*").set_defaults(func=cmd_plots)
    sub.add_parser("article", help="findings+plots -> Publish/article/*").set_defaults(func=cmd_article)
    sub.add_parser("landing", help="findings+plots -> Publish/landing/*").set_defaults(func=cmd_landing)
    sub.add_parser("publish", help="derive && plots && article && landing && manifest").set_defaults(func=cmd_publish)
    cp = sub.add_parser("compare", help="scoped head-to-head bundles: FluentCart vs one competitor")
    cp.add_argument("--vs", choices=["woo", "edd", "all"], default="all",
                    help="which competitor (default: both)")
    cp.set_defaults(func=cmd_compare)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
