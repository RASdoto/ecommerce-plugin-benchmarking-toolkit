"""
BaseModule — Python port of `module/BaseConfiguration.js` (parity P5–P9).

Responsibilities:
  * load post.json (body) and, conditionally, query.json (query params)
  * build the request URL (base_url join vs ignore_base_url; append query params)
  * expose method / headers / body and the per-request `modify_request_body` hook
  * produce the resolved options dict consumed by the async load engine
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from .config import ConfigResolver


class RequestSpec:
    """Immutable-ish description of what the load engine should send."""

    def __init__(
        self,
        url: str,
        method: str,
        headers: dict,
        body: Any,
        max_requests: int,
        concurrency: int,
        insecure: bool,
        quiet: bool,
        dump_body: bool,
        content_type: str,
        module,
    ):
        self.url = url
        self.method = method.upper()
        self.headers = headers
        self.body = body
        self.max_requests = max_requests
        self.concurrency = concurrency
        self.insecure = insecure
        self.quiet = quiet
        self.dump_body = dump_body
        self.content_type = content_type
        self.module = module  # back-reference for modify_request_body


class BaseModule:
    """Base class for all platform modules.

    Subclasses override `method`, `headers`, `build_url`, and
    `modify_request_body` as needed — mirroring the Node subclasses.

    `secret(key)` is injected by the caller to resolve credentials
    (replaces the Node modules' direct `process.env` reads).
    """

    default_method = "POST"

    def __init__(
        self,
        module_path: str,
        app_config: dict,
        data_dir: str | Path,
        secret: Callable[[str, str | None], Any] | None = None,
    ):
        self.module_path = module_path.strip("/")
        self.module_name = self.module_path.replace("/", ".")
        self.cfg = ConfigResolver(app_config, self.module_name)
        self.data_dir = Path(data_dir) / self.module_path
        self._secret = secret or (lambda k, d=None: d)

        self.post_data = self._load_json("post.json", default={})
        # query.json loaded only for GET or when append_url_params is set (parity P5)
        if (self.cfg.get("append_url_params") or self.method().lower() == "get"):
            self.query_params = self._load_json("query.json", default={})
        else:
            self.query_params = {}

    # ---- overridable behaviour -------------------------------------------

    def method(self) -> str:
        return self.default_method

    def get_post_body(self) -> Any:
        return self.post_data

    def modify_request_body(self, index: int, body: Any) -> Any:
        """Per-request mutation hook (parity P8). Identity by default.

        NOTE: the Node tool mutated a shared object reference, causing appended
        indices to accumulate. This port deep-copies per request so each index
        is clean (documented deviation). Subclasses receive a fresh copy.
        """
        return body

    def headers(self) -> dict:
        """Extra headers (auth, etc.). Overridden per platform."""
        return {}

    def get_url(self) -> str:
        if self.cfg.get("ignore_base_url", False):
            return self.cfg.get("url")
        return (self.cfg.base_url or "") + (self.cfg.get("url") or "")

    # ---- resolved spec ----------------------------------------------------

    def build_url(self) -> str:
        raw = self.get_url()
        if not self.query_params:
            return raw
        parts = urlsplit(raw)
        existing = dict(parse_qsl(parts.query, keep_blank_values=True))
        existing.update({k: str(v) for k, v in self.query_params.items()})
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment)
        )

    def to_spec(self, max_requests_override: int | None = None) -> RequestSpec:
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(self.headers())
        return RequestSpec(
            url=self.build_url(),
            method=self.method(),
            headers=hdrs,
            body=self.get_post_body(),
            max_requests=int(
                max_requests_override
                if max_requests_override is not None
                else self.cfg.get("max_request", 1000)
            ),
            concurrency=int(self.cfg.get("concurrency", 140)),
            insecure=bool(self.cfg.get("insecure", False)),
            quiet=bool(self.cfg.get("quiet", False)),
            dump_body=bool(self.cfg.get("dump_body", False)),
            content_type="application/json",
            module=self,
        )

    def render_body(self, index: int) -> Any:
        """Return the body for request `index` (1-based), deep-copied + mutated."""
        base = copy.deepcopy(self.get_post_body())
        return self.modify_request_body(index, base)

    # ---- helpers ----------------------------------------------------------

    def secret(self, key: str, default: str | None = None):
        return self._secret(key, default)

    def _load_json(self, name: str, default: Any):
        path = self.data_dir / name
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                return default
        return default
