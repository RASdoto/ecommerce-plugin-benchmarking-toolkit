"""
Bootstrap / auto-provisioner (Phase 0b, [SSH/REST]).

Turns "URL + credentials" into a fully prepared environment. Per site, idempotently:
  1. connect over SSH
  2. ensure WP-CLI (install wp-cli.phar if missing)
  3. resolve WP paths (root, wp-content, table prefix)
  4. deploy bench-probe  (mu-plugin via SSH  OR  normal plugin via REST app pw)
  5. ensure an Application Password (mint via WP-CLI if only a login pw was given)
  6. capture the admin session cookie via headless login
  7. snapshot environment (PHP/MySQL/etc.)
  8. write bootstrap_state to .secrets/

Everything is stored in settings' derived-secrets store so re-runs are no-ops.
"""
from __future__ import annotations

import io
import json
import secrets as _secrets
import zipfile
from pathlib import Path
from typing import Optional

import requests

from ..sshutil import from_site, SSHError

WP_CLI_URL = "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar"


class BootstrapResult(dict):
    pass


class Bootstrapper:
    def __init__(self, settings, probe_php_path: str, edd_api_php_path: str = ""):
        self.s = settings
        self.probe_php_path = probe_php_path
        self.edd_api_php_path = edd_api_php_path

    # ---------------------------------------------------------------- site
    def provision_site(self, site_key: str) -> BootstrapResult:
        site = self.s.sites[site_key]
        state = self.s.load_derived(site_key)
        res = BootstrapResult(site=site_key, steps={}, fallbacks=[])

        bench_secret = state.get("bench_secret") or _secrets.token_hex(16)
        res["bench_secret"] = bench_secret

        ssh = None
        wp = "wp"
        wp_path = state.get("wp_path", "")
        have_ssh = bool(site.ssh_host and site.ssh_user and (site.ssh_key or site.ssh_pass))

        if have_ssh:
            try:
                ssh = from_site(site)
                ssh.connect()
                res["steps"]["ssh"] = "ok"
            except Exception as exc:
                res["steps"]["ssh"] = f"failed: {exc}"
                res["fallbacks"].append("no-ssh")
                ssh = None

        if ssh is not None:
            wp, wp_path = self._ensure_wpcli(ssh, res)
            self._deploy_probe_ssh(ssh, wp, wp_path, bench_secret, res)
            app_pw = self._ensure_app_password_wpcli(ssh, wp, wp_path, site, res)
        else:
            app_pw = site.app_password
            # profiler via REST (needs an app password)
            if not app_pw:
                res["steps"]["app_password"] = "missing (no SSH, no app pw) — cannot mint"
                res["fallbacks"].append("no-app-password")
            else:
                self._deploy_probe_rest(site, app_pw, bench_secret, res)

        # EDD site: also deploy the bench-edd-api helper plugin (REST create endpoints)
        if site_key == "edd" and self.edd_api_php_path:
            self._deploy_edd_api(ssh, wp, wp_path, site, app_pw, res)

        # verify profiler responds
        res["steps"]["probe_verify"] = self._verify_probe(site, bench_secret)

        # capture admin cookie / storage_state
        cookie = self._capture_admin(site, res)

        # environment snapshot
        if ssh is not None and wp_path:
            res["env"] = self._env_snapshot(ssh, wp, wp_path)

        if ssh is not None:
            ssh.close()

        # persist derived secrets
        derived = {
            "bench_secret": bench_secret,
            "wp_path": wp_path,
            "wp_cli": wp,
            "provisioned": True,
        }
        if app_pw:
            derived["app_password"] = app_pw
            # expose common REST auth keys for load modules
            derived.setdefault("REST_APP_PASSWORD", app_pw)
        if cookie:
            derived["cookie_header"] = cookie
        self.s.save_derived(site_key, derived)
        res["ok"] = "no-app-password" not in res["fallbacks"] or bool(cookie)
        return res

    # ---------------------------------------------------------------- steps
    def _ensure_wpcli(self, ssh, res) -> tuple[str, str]:
        code, out, _ = ssh.run("wp --info --allow-root 2>/dev/null || wp --info 2>/dev/null")
        wp = "wp"
        if code != 0:
            # install to ~/bin/wp
            ssh.run("mkdir -p ~/bin")
            ssh.run(f"curl -sSL {WP_CLI_URL} -o ~/bin/wp || wget -q {WP_CLI_URL} -O ~/bin/wp")
            ssh.run("chmod +x ~/bin/wp")
            wp = "~/bin/wp"
            code2, _, _ = ssh.run(f"{wp} --info 2>/dev/null")
            res["steps"]["wp_cli"] = "installed" if code2 == 0 else "install-failed"
        else:
            res["steps"]["wp_cli"] = "present"

        # find WP root: search common web roots for wp-config.php.
        # ~/files first (cPanel bind-mounted site dir — writeable by the user);
        # falls back to server-owned /var/www paths only via `find` below.
        wp_path = ""
        for guess in ("~/files", "~/public_html", "/var/www/html", "~/www", "~/htdocs", "."):
            c, o, _ = ssh.run(f"test -f {guess}/wp-config.php && echo {guess}")
            if c == 0 and o.strip():
                wp_path = o.strip()
                break
        if not wp_path:
            c, o, _ = ssh.run(
                "find ~ /var/www -maxdepth 4 -name wp-config.php 2>/dev/null | head -1"
            )
            if o.strip():
                wp_path = o.strip().rsplit("/", 1)[0]
        res["steps"]["wp_path"] = wp_path or "not-found"
        return wp, wp_path

    def _deploy_probe_ssh(self, ssh, wp, wp_path, bench_secret, res) -> None:
        if not wp_path:
            res["steps"]["probe_install"] = "skipped (no wp_path)"
            res["fallbacks"].append("probe-ssh-nopath")
            return
        php = Path(self.probe_php_path).read_text()
        php = php.replace("REPLACE_ME_BENCH_SECRET", bench_secret)
        mu_dir = f"{wp_path}/wp-content/mu-plugins"
        try:
            ssh.run(f"mkdir -p {mu_dir}")
            ssh.put_text(php, f"{mu_dir}/bench-probe.php")
            res["steps"]["probe_install"] = "mu-plugin (ssh)"
        except SSHError as exc:
            res["steps"]["probe_install"] = f"ssh-write-failed: {exc}"
            res["fallbacks"].append("probe-ssh-write")

    def _deploy_probe_rest(self, site, app_pw, bench_secret, res) -> None:
        """Upload + activate bench-probe as a normal plugin via REST app pw."""
        php = Path(self.probe_php_path).read_text().replace(
            "REPLACE_ME_BENCH_SECRET", bench_secret
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("bench-probe/bench-probe.php", php)
        buf.seek(0)
        url = site.rest_url + "wp/v2/plugins"
        try:
            r = requests.post(
                url,
                headers={
                    "Content-Disposition": "attachment; filename=bench-probe.zip",
                    "Content-Type": "application/zip",
                },
                data=buf.read(),
                auth=(site.admin_user, app_pw),
                verify=False,
                timeout=60,
            )
            if r.status_code in (200, 201):
                # activate
                slug = "bench-probe/bench-probe"
                requests.put(
                    site.rest_url + f"wp/v2/plugins/{slug}",
                    json={"status": "active"},
                    auth=(site.admin_user, app_pw),
                    verify=False, timeout=60,
                )
                res["steps"]["probe_install"] = "plugin (rest)"
            else:
                res["steps"]["probe_install"] = f"rest-upload {r.status_code}"
                res["fallbacks"].append("probe-rest")
        except requests.RequestException as exc:
            res["steps"]["probe_install"] = f"rest-error: {exc}"
            res["fallbacks"].append("probe-rest")

    def _deploy_edd_api(self, ssh, wp, wp_path, site, app_pw, res) -> None:
        """Install + activate the bench-edd-api helper plugin on the EDD site.

        Prefers SSH/SFTP + `wp plugin activate`; falls back to REST plugin upload.
        """
        php = Path(self.edd_api_php_path).read_text()
        # SSH path
        if ssh is not None and wp_path:
            try:
                pdir = f"{wp_path}/wp-content/plugins/bench-edd-api"
                ssh.run(f"mkdir -p {pdir}")
                ssh.put_text(php, f"{pdir}/bench-edd-api.php")
                code, out, err = ssh.run(
                    f"cd {wp_path} && {wp} plugin activate bench-edd-api 2>&1")
                res["steps"]["edd_api_install"] = (
                    "plugin (ssh, activated)" if code == 0 else f"ssh-activate: {out.strip()[:80]}")
                return
            except SSHError as exc:
                res["steps"]["edd_api_install"] = f"ssh-write-failed: {exc}"
        # REST fallback (needs app password)
        if not app_pw:
            res["steps"]["edd_api_install"] = "skipped (no ssh, no app pw)"
            res["fallbacks"].append("edd-api")
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("bench-edd-api/bench-edd-api.php", php)
        buf.seek(0)
        try:
            r = requests.post(
                site.rest_url + "wp/v2/plugins",
                headers={"Content-Disposition": "attachment; filename=bench-edd-api.zip",
                         "Content-Type": "application/zip"},
                data=buf.read(), auth=(site.admin_user, app_pw),
                verify=False, timeout=60,
            )
            if r.status_code in (200, 201):
                requests.put(
                    site.rest_url + "wp/v2/plugins/bench-edd-api/bench-edd-api",
                    json={"status": "active"},
                    auth=(site.admin_user, app_pw), verify=False, timeout=60,
                )
                res["steps"]["edd_api_install"] = "plugin (rest, activated)"
            else:
                res["steps"]["edd_api_install"] = f"rest-upload {r.status_code}"
                res["fallbacks"].append("edd-api")
        except requests.RequestException as exc:
            res["steps"]["edd_api_install"] = f"rest-error: {exc}"
            res["fallbacks"].append("edd-api")

    def _ensure_app_password_wpcli(self, ssh, wp, wp_path, site, res) -> str:
        if site.app_password:
            res["steps"]["app_password"] = "provided"
            return site.app_password
        if not wp_path:
            res["steps"]["app_password"] = "skipped (no wp_path)"
            return ""
        cmd = (
            f"cd {wp_path} && {wp} user application-password create "
            f"{site.admin_user} bench --porcelain 2>/dev/null"
        )
        code, out, _ = ssh.run(cmd)
        pw = out.strip().splitlines()[-1] if out.strip() else ""
        if code == 0 and pw:
            res["steps"]["app_password"] = "minted"
            return pw
        res["steps"]["app_password"] = "mint-failed"
        return ""

    def _verify_probe(self, site, bench_secret) -> str:
        try:
            r = requests.get(
                site.url,
                headers={"X-Bench": "1", "X-Bench-Secret": bench_secret},
                verify=False, timeout=30,
            )
            if "X-Bench-Query-Count" in r.headers or "Server-Timing" in r.headers:
                return "ok"
            return "no-headers"
        except requests.RequestException as exc:
            return f"error: {exc}"

    def _capture_admin(self, site, res) -> str:
        if not (site.admin_user and site.admin_pass):
            res["steps"]["admin_cookie"] = "skipped (no admin login)"
            return ""
        try:
            from ..probes.browser import BrowserProbe
            out_path = str(self.s.secrets_dir / f"{site.key}_storage_state.json")
            bp = BrowserProbe()
            info = bp.capture_login(site.admin_url, site.admin_user, site.admin_pass, out_path)
            res["steps"]["admin_cookie"] = "captured"
            self.s.save_derived(site.key, {"storage_state": out_path})
            return info.get("cookie_header", "")
        except Exception as exc:
            res["steps"]["admin_cookie"] = f"failed: {exc}"
            res["fallbacks"].append("no-admin-cookie")
            return ""

    def _env_snapshot(self, ssh, wp, wp_path) -> dict:
        def one(cmd):
            _, o, _ = ssh.run(f"cd {wp_path} && {cmd} 2>/dev/null")
            return o.strip()
        return {
            "php_version": one(f"{wp} eval 'echo PHP_VERSION;'"),
            "wp_version": one(f"{wp} core version"),
            "mysql_version": one(f"{wp} db query 'SELECT VERSION();' --skip-column-names"),
            "db_prefix": one(f"{wp} config get table_prefix"),
        }

    # ---------------------------------------------------------------- all
    def provision_all(self) -> dict:
        out = {}
        for key in self.s.sites:
            out[key] = self.provision_site(key)
        state_path = self.s.secrets_dir / "bootstrap_state.json"
        state_path.write_text(json.dumps(out, indent=2, default=str))
        return out
