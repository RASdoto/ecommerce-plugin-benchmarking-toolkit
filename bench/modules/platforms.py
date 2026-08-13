"""
Platform module classes — 1:1 ports of the Node `module/**/config.js` subclasses
(parity P7 + auth P13–P16). Behaviour differences per module are limited to
method, headers (auth), URL shaping, and per-request body mutation.

Credentials come via `self.secret(key, default)` (replaces process.env reads).
"""
from __future__ import annotations

import base64
import uuid

from ..core.module_base import BaseModule


def _basic(user: str, pw: str) -> str:
    raw = f"{user or ''}:{pw or ''}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _uniq() -> str:
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- WP
class WpModule(BaseModule):
    def method(self) -> str:
        return "GET"


# -------------------------------------------------------------------- FluentCart
# Admin endpoints (fluent-cart/v2/products, /orders, /customers) use WordPress
# Application Password over HTTP Basic Auth. Money is in CENTS.
class _AdminBasicAuth:
    """Mixin: WP admin Basic auth from admin_user + application password.

    Used by FluentCart admin endpoints and the EDD helper-plugin endpoints.
    """
    def headers(self) -> dict:
        user = self.secret("ADMIN_USER", "")
        app_pw = self.secret("APP_PASSWORD", "") or self.secret("REST_APP_PASSWORD", "")
        if user and app_pw:
            return {"Authorization": _basic(user, app_pw)}
        return {}


# Backwards-compatible alias
_FCAdminAuth = _AdminBasicAuth


class FluentCartProductsCreate(_AdminBasicAuth, BaseModule):
    # POST fluent-cart/v2/products  {post_title, post_status, detail:{fulfillment_type}}
    def method(self) -> str:
        return "POST"

    def modify_request_body(self, index: int, body):
        body["post_title"] = f"{body.get('post_title', 'Bench Product')} {index}-{_uniq()}"
        return body


class FluentCartOrderCreate(_AdminBasicAuth, BaseModule):
    # POST fluent-cart/v2/orders  (admin create — no session cart needed).
    # Requires a real customer_id + order_items referencing a real product
    # variation. IDs are resolved during seeding into .secrets and injected here.
    def method(self) -> str:
        return "POST"

    def modify_request_body(self, index: int, body):
        cid = self.secret("FC_CUSTOMER_ID", "")
        pid = self.secret("FC_PRODUCT_ID", "")
        vid = self.secret("FC_VARIATION_ID", "")
        price = self.secret("FC_UNIT_PRICE", "")
        if cid:
            body["customer_id"] = int(cid)
        if body.get("order_items"):
            item = body["order_items"][0]
            if pid:
                item["post_id"] = int(pid)
            if vid:
                item["variation_id"] = int(vid)
                item["object_id"] = int(vid)
            # keep all money fields reconciled (FC validates subtotal == item totals)
            if price:
                p = int(price)
                qty = int(item.get("quantity", 1) or 1)
                line = p * qty
                item.update(unit_price=p, price=p, item_total=line,
                            total=line, line_total=line)
                body["subtotal"] = line
                body["total_amount"] = line
        return body


class FluentCartProductsList(BaseModule):
    # GET fluent-cart/v2/public/products  — public, no auth required.
    def method(self) -> str:
        return "GET"


# --------------------------------------------------------------------- WooCommerce
class _WooBasicAuth(BaseModule):
    def headers(self) -> dict:
        return {
            "Authorization": _basic(
                self.secret("WOO_CONSUMER_KEY", ""),
                self.secret("WOO_CONSUMER_SECRET", ""),
            )
        }


class WooOrderCreate(_WooBasicAuth):
    def method(self) -> str:
        return "POST"

    def modify_request_body(self, index: int, body):
        # The static post.json line item hardcodes product_id 91, which does not
        # survive a catalogue reset. Inject a resolved product id (persisted to
        # .secrets by ensure_woo_order_refs during seeding) when available.
        pid = self.secret("WOO_PRODUCT_ID", "")
        if pid and body.get("line_items"):
            body["line_items"][0]["product_id"] = int(pid)
        return body


class WooProductsCreate(_WooBasicAuth):
    def method(self) -> str:
        return "POST"


class WooProductsList(_WooBasicAuth):
    def method(self) -> str:
        return "GET"


# ---------------------------------------------------------------------- SureCart
class SureCartOrderCreate(BaseModule):
    def method(self) -> str:
        return "POST"

    def headers(self) -> dict:
        return {"Authorization": "Bearer " + str(self.secret("SURE_API_KEY", ""))}


class SureCartProductsCreate(BaseModule):
    def method(self) -> str:
        return "POST"

    def headers(self) -> dict:
        return {
            "Authorization": _basic(
                self.secret("USERNAME", ""), self.secret("PASSWORD", "")
            )
        }

    def modify_request_body(self, index: int, body):
        # Node appends the index to name/price_name for uniqueness.
        name = f"{body.get('name', 'Product')} {index}"
        body["name"] = name
        body["price_name"] = f"{name} {index}"
        return body


class SureCartProductsList(BaseModule):
    def method(self) -> str:
        return "GET"

    def headers(self) -> dict:
        return {"Authorization": "Bearer " + str(self.secret("SURE_API_KEY", ""))}


# --------------------------------------------------------------------------- EDD
# Create ops hit the bench-edd-api helper plugin (namespaces edd-cus/v1 and
# custom-api/v1). Authenticated with the WP admin application password.
class EddOrderCreate(_AdminBasicAuth, BaseModule):
    def method(self) -> str:
        return "POST"
    # The helper plugin resolves a real download id server-side if items[].id
    # doesn't exist, and generates a unique purchase_key — no client work needed.


class EddCustomerCreate(_AdminBasicAuth, BaseModule):
    def method(self) -> str:
        return "POST"

    def modify_request_body(self, index: int, body):
        # unique email + login per request (helper plugin also de-dupes)
        u = _uniq()
        body["email"] = f"benchcust{index}-{u}@example.com"
        body["user_login"] = f"benchcust{index}-{u}"
        body["post_title"] = f"{body.get('post_title', 'Customer')} {index}"
        return body


class EddProductCreate(_AdminBasicAuth, BaseModule):
    def method(self) -> str:
        return "POST"

    def modify_request_body(self, index: int, body):
        body["post_title"] = f"{body.get('post_title', 'Product')} {index}-{_uniq()}"
        return body


class EddProductList(BaseModule):
    def method(self) -> str:
        return "GET"

    def get_url(self) -> str:
        # Legacy EDD API (edd-api/*) sits at the site root, NOT under /wp-json/.
        # base_url is the platform REST URL (e.g. https://host/wp-json/), so
        # strip the /wp-json/ suffix before joining the config path.
        if self.cfg.get("ignore_base_url", False):
            base = self.cfg.get("url") or ""
        else:
            root = (self.cfg.base_url or "").rstrip("/")
            if root.endswith("/wp-json"):
                root = root[: -len("/wp-json")]
            base = root + "/" + (self.cfg.get("url") or "").lstrip("/")
        key = self.secret("EDD_API_KEY", "")
        token = self.secret("EDD_API_TOKEN", "")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}key={key}&token={token}"
