"""
Settings — loads the two user files (credentials.yaml, matrix.yaml), derives the
internal per-site config, and resolves secrets for modules.

credentials.yaml is the ONLY file the user edits. Derived/minted secrets live in
.secrets/ (agent-managed) and are merged in at resolution time.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


PLATFORMS = ("fluentcart", "woo", "edd")

# maps a module path prefix -> platform key in credentials.yaml
MODULE_PLATFORM = {
    "fluent-cart": "fluentcart",
    "woo": "woo",
    "sure-cart": "woo",   # surecart uses its own keys but shares no bench site here
    "edd": "edd",
    "wp": "fluentcart",
}


@dataclass
class SiteConfig:
    key: str
    url: str
    admin_user: str = ""
    admin_pass: str = ""
    app_password: str = ""
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key: str = ""
    ssh_pass: str = ""
    api: dict = field(default_factory=dict)  # WOO_CONSUMER_KEY, SURE_API_KEY, etc.

    @property
    def admin_url(self) -> str:
        return self.url.rstrip("/") + "/wp-admin/"

    @property
    def rest_url(self) -> str:
        return self.url.rstrip("/") + "/wp-json/"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("pyyaml is required (pip install pyyaml)")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Settings:
    def __init__(self, root: str | Path = "."):
        self.root = Path(root)
        self.credentials_path = self.root / "credentials.yaml"
        self.matrix_path = self.root / "matrix.yaml"
        self.config_json_path = self.root / "config.json"
        self.module_dir = self.root / "module"
        self.reports_dir = self.root / "reports"
        self.secrets_dir = self.root / ".secrets"
        self.secrets_dir.mkdir(exist_ok=True)
        self.reports_dir.mkdir(exist_ok=True)

        self._creds = _load_yaml(self.credentials_path)
        self.matrix = _load_yaml(self.matrix_path)
        self.sites: dict[str, SiteConfig] = self._build_sites()

    # ---- sites -----------------------------------------------------------
    def _build_sites(self) -> dict[str, SiteConfig]:
        out: dict[str, SiteConfig] = {}
        for key in PLATFORMS:
            block = self._creds.get(key)
            if not block:
                continue
            out[key] = SiteConfig(
                key=key,
                url=str(block.get("url", "")).rstrip("/"),
                admin_user=str(block.get("admin_user", "")),
                admin_pass=str(block.get("admin_pass", "")),
                app_password=str(block.get("app_password", "") or ""),
                ssh_host=str(block.get("ssh_host", "")),
                ssh_port=int(block.get("ssh_port", 22) or 22),
                ssh_user=str(block.get("ssh_user", "")),
                ssh_key=str(block.get("ssh_key", "") or ""),
                ssh_pass=str(block.get("ssh_pass", "") or ""),
                api=dict(block.get("api", {}) or {}),
            )
        return out

    def platform_for_module(self, module_path: str) -> str:
        prefix = module_path.strip("/").split("/", 1)[0]
        return MODULE_PLATFORM.get(prefix, "fluentcart")

    # ---- derived secrets store ------------------------------------------
    def _derived_path(self, site_key: str) -> Path:
        return self.secrets_dir / f"{site_key}.json"

    def load_derived(self, site_key: str) -> dict:
        p = self._derived_path(site_key)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def save_derived(self, site_key: str, data: dict) -> None:
        existing = self.load_derived(site_key)
        existing.update(data)
        self._derived_path(site_key).write_text(json.dumps(existing, indent=2))

    def bench_secret(self, site_key: str) -> str:
        return self.load_derived(site_key).get("bench_secret", "")

    # ---- secret resolution for modules ----------------------------------
    def secret_resolver(self, site_key: str):
        """Return a callable secret(key, default) for a given site.

        Resolution order: credentials.api -> derived (.secrets) -> env -> default.
        """
        site = self.sites.get(site_key)
        api = dict(site.api) if site else {}
        derived = self.load_derived(site_key)
        # expose site identity/credentials as resolvable secrets (for Basic auth, etc.)
        builtin = {}
        if site:
            builtin["ADMIN_USER"] = site.admin_user
            builtin["APP_PASSWORD"] = site.app_password or derived.get("app_password", "")
            builtin["ADMIN_PASS"] = site.admin_pass

        def resolve(key: str, default: Optional[str] = None):
            if key in api:
                return api[key]
            if key in derived:
                return derived[key]
            if key in builtin and builtin[key]:
                return builtin[key]
            env = os.environ.get(key)
            if env is not None:
                return env
            return default

        return resolve

    # ---- matrix knobs with capped defaults ------------------------------
    def matrix_get(self, key: str, default: Any) -> Any:
        return self.matrix.get(key, default)
