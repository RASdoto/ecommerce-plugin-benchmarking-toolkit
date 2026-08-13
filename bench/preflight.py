"""
Preflight validation (fail-fast). Confirms config is loadable, sites are
reachable over HTTP, bootstrap has completed (profiler responding, app pw +
cookie present), and reports precise reasons on failure.
"""
from __future__ import annotations

import requests

from .settings import Settings


def check(settings: Settings) -> dict:
    report = {"ok": True, "sites": {}, "problems": []}

    if not settings.config_json_path.exists():
        report["ok"] = False
        report["problems"].append("config.json missing")

    if not settings.sites:
        report["ok"] = False
        report["problems"].append("credentials.yaml has no site blocks")

    for key, site in settings.sites.items():
        s = {"url": site.url}
        # HTTP reachability
        try:
            r = requests.get(site.url, verify=False, timeout=20)
            s["http"] = r.status_code
        except requests.RequestException as exc:
            s["http"] = f"unreachable: {exc}"
            report["ok"] = False
            report["problems"].append(f"{key}: site unreachable")

        derived = settings.load_derived(key)
        s["provisioned"] = bool(derived.get("provisioned"))
        s["has_bench_secret"] = bool(derived.get("bench_secret"))
        s["has_app_password"] = bool(derived.get("app_password"))
        s["has_cookie"] = bool(derived.get("cookie_header") or derived.get("storage_state"))

        # profiler live check
        secret = derived.get("bench_secret", "")
        if secret:
            try:
                r = requests.get(site.url, headers={"X-Bench": "1", "X-Bench-Secret": secret},
                                 verify=False, timeout=20)
                s["profiler"] = ("ok" if ("X-Bench-Query-Count" in r.headers
                                          or "Server-Timing" in r.headers) else "no-headers")
            except requests.RequestException as exc:
                s["profiler"] = f"error: {exc}"
        else:
            s["profiler"] = "not-provisioned"

        if not s["provisioned"]:
            report["problems"].append(f"{key}: not bootstrapped (run `bench bootstrap`)")
        report["sites"][key] = s

    return report
