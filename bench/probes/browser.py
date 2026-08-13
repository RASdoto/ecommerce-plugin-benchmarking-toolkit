"""
Browser automation ([BR], Phase 7) via Playwright (sync API).

Owns the metrics the HTTP engine cannot reach:
  * rendered admin/list timing (DOMContentLoaded, LCP) incl. JS execution
  * bulk-action completion time (nonce + real UI + data mutation)
  * storefront LCP / JS weight

Login is captured once to a storage_state file (also reused as the auth cookie
for [LT+P] admin requests).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


class BrowserProbe:
    def __init__(self, storage_state: Optional[str] = None, headless: bool = True):
        self.storage_state = storage_state
        self.headless = headless

    # ---- login / cookie capture -----------------------------------------
    def capture_login(self, admin_url: str, user: str, password: str,
                      out_path: str) -> dict:
        """Log into wp-admin and save storage_state (cookies + origins)."""
        if sync_playwright is None:
            raise RuntimeError("playwright not installed (pip install playwright; playwright install chromium)")
        login_url = admin_url.rstrip("/").replace("/wp-admin", "") + "/wp-login.php"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()
            page.goto(login_url, wait_until="domcontentloaded")
            page.fill("#user_login", user)
            page.fill("#user_pass", password)
            page.click("#wp-submit")
            page.wait_for_load_state("networkidle")
            ctx.storage_state(path=out_path)
            cookies = ctx.cookies()
            browser.close()
        self.storage_state = out_path
        # extract the logged_in cookie for [LT+P] admin requests
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies
            if c["name"].startswith("wordpress_logged_in")
            or c["name"].startswith("wordpress_sec")
        )
        return {"storage_state": out_path, "cookie_header": cookie_header}

    # ---- rendered page timing -------------------------------------------
    def page_timing(self, url: str, wait: str = "networkidle") -> dict:
        if sync_playwright is None:
            raise RuntimeError("playwright not installed")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(
                storage_state=self.storage_state, ignore_https_errors=True
            )
            page = ctx.new_page()
            js_bytes = {"total": 0}

            def _on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    if "javascript" in ct:
                        body = resp.body()
                        js_bytes["total"] += len(body)
                except Exception:
                    pass

            page.on("response", _on_response)
            page.goto(url, wait_until=wait, timeout=60000)
            timing = page.evaluate(
                """() => {
                    const n = performance.getEntriesByType('navigation')[0] || {};
                    let lcp = 0;
                    const l = performance.getEntriesByType('largest-contentful-paint');
                    if (l && l.length) lcp = l[l.length-1].startTime;
                    return {
                        ttfb: n.responseStart || 0,
                        dom_content_loaded: n.domContentLoadedEventEnd || 0,
                        load: n.loadEventEnd || 0,
                        lcp: lcp
                    };
                }"""
            )
            reqs = page.evaluate("() => performance.getEntriesByType('resource').length")
            browser.close()
        return {
            "ttfb_ms": round(timing.get("ttfb", 0), 1),
            "dom_content_loaded_ms": round(timing.get("dom_content_loaded", 0), 1),
            "load_ms": round(timing.get("load", 0), 1),
            "lcp_ms": round(timing.get("lcp", 0), 1),
            "requests": reqs,
            "js_kb": round(js_bytes["total"] / 1024, 1),
        }

    # ---- bulk action timing ---------------------------------------------
    def bulk_action_timing(self, list_url: str, select_all_sel: str,
                           action_value: str, apply_sel: str,
                           action_select_sel: str = "#bulk-action-selector-top") -> dict:
        """Time a bulk action in a WP list table. Returns wall time in ms.

        Selectors are platform-specific and passed in by the runner.
        """
        if sync_playwright is None:
            raise RuntimeError("playwright not installed")
        import time
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(
                storage_state=self.storage_state, ignore_https_errors=True
            )
            page = ctx.new_page()
            page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
            page.check(select_all_sel)
            page.select_option(action_select_sel, action_value)
            t0 = time.perf_counter()
            page.click(apply_sel)
            page.wait_for_load_state("networkidle", timeout=120000)
            elapsed = (time.perf_counter() - t0) * 1000.0
            browser.close()
        return {"bulk_ms": round(elapsed, 1)}
