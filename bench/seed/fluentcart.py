"""
FluentCart REST seeder + order-create reference resolver.

FluentCart's beta `/fluent-cart/seed` endpoint is gone; creation now goes through
the documented admin REST API (namespace `fluent-cart/v2`, WP Application Password
Basic auth, money in CENTS):

  * products  -> POST /products/bulk-insert  {products:[{post_title, detail:{fulfillment_type}}]}
  * customers -> POST /customers             {email, first_name, ..., wp_user:"yes"}
  * orders    -> POST /orders                {customer_id, order_items:[{post_id,variation_id,
                 object_id,quantity,unit_price,...}], total_amount, payment_method, ...}

Order-create needs REAL ids: a product `post_id`, its `variation_id` (from the admin
`/variants` endpoint — the ONLY place variation ids are exposed; the public product
list does not include them), and a `customer_id`. `resolve_refs()` fetches those and,
if the site has none, CREATES a product + customer first, then resolves. Rate-limited
(token bucket + backoff) — seed-only.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

import requests

from .seeder import CEILINGS, SeedThrottle, TokenBucket


# Server-side product generator (run via `wp eval-file <file> <count>`).
# The public REST create/bulk-insert does NOT persist a variation price, so
# products created that way can't back an order. Creating via FluentCart's own
# models writes a properly-priced, indexed variation (fct_product_variations
# .item_price) so /variants returns it and order totals compute correctly.
FC_SEED_PRODUCT_PHP = r"""<?php
$count = isset($args[0]) ? (int)$args[0] : 1;
$price = isset($args[1]) ? (int)$args[1] : 2500;
$P = 'FluentCart\\App\\Models\\Product';
$D = 'FluentCart\\App\\Models\\ProductDetail';
$V = 'FluentCart\\App\\Models\\ProductVariation';
if (!class_exists($V) || !class_exists($D)) { echo 'NO_FC_MODELS'; return; }
for ($i = 0; $i < $count; $i++) {
    $post_id = wp_insert_post(array(
        'post_title'  => 'Bench Product ' . uniqid(),
        'post_status' => 'publish',
        'post_type'   => 'fluent-products',
        'post_author' => 1,
    ), true);
    if (is_wp_error($post_id)) { continue; }
    $v = new $V;
    $v->post_id = $post_id;
    $v->serial_index = 0;
    $v->variation_title = 'Default';
    $v->variation_identifier = 'default';
    $v->payment_type = 'onetime';
    $v->fulfillment_type = 'digital';
    $v->item_status = 'active';
    $v->item_price = $price;
    $v->manage_stock = 'no';
    $v->save();
    $d = new $D;
    $d->post_id = $post_id;
    $d->fulfillment_type = 'digital';
    $d->variation_type = 'simple';
    $d->min_price = $price;
    $d->max_price = $price;
    $d->default_variation_id = $v->id;
    $d->manage_stock = 'no';
    $d->save();
}
echo 'OK';
"""


class FluentCartSeeder:
    def __init__(self, rest_base: str, admin_user: str, app_password: str,
                 throttle: Optional[SeedThrottle] = None, verify_tls: bool = False,
                 ssh=None, wp_path: str = "", wp_cli: str = "wp"):
        # rest_base like https://site/wp-json/  -> we append fluent-cart/v2/...
        self.base = rest_base.rstrip("/") + "/fluent-cart/v2"
        self.auth = (admin_user, app_password)
        self.throttle = throttle or SeedThrottle()
        self.verify = verify_tls
        self.events: list[str] = []
        # SSH is used to create PRICED products via FluentCart models (the only
        # reliable way — REST can't persist a variation price). Customers/orders
        # still go over REST.
        self.ssh = ssh
        self.wp_path = wp_path
        self.wp_cli = wp_cli
        self._product_script_remote = ""
        self.default_price = 2500
        # abort a seed loop after this many consecutive non-advancing attempts
        # (prevents spinning forever on a persistent 4xx / auth reject)
        self.max_failures = 5

    @property
    def use_wpcli_products(self) -> bool:
        return self.ssh is not None and bool(self.wp_path)

    # ---- generic GET helpers --------------------------------------------
    def _get_first(self, path: str, params: Optional[dict] = None,
                   authed: bool = True) -> Optional[dict]:
        """GET path and return the first record dict from whatever envelope
        FluentCart wraps it in (bare list, {data:[]}, {variants:[]}, nested
        products.products.data, ...)."""
        try:
            r = requests.get(self.base + path, params=params or {"per_page": 1},
                             auth=self.auth if authed else None,
                             verify=self.verify, timeout=30)
            items = _first_list(r.json())
            return items[0] if items else None
        except Exception as exc:
            self.events.append(f"GET {path} failed: {exc}")
            return None

    # ---- counts ----------------------------------------------------------
    def current_count(self, entity: str) -> Optional[int]:
        # products: prefer a direct DB count over WP-CLI (reliable + reflects
        # WP-CLI-created rows immediately) when SSH is available.
        if entity == "products" and self.use_wpcli_products:
            try:
                cmd = (f"cd {self.wp_path} && {self.wp_cli} eval "
                       "'global $wpdb;echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM "
                       "{$wpdb->prefix}fct_product_details\");' 2>/dev/null")
                _, out, _ = self.ssh.run(cmd)
                return int(out.strip().splitlines()[-1])
            except Exception:
                pass
        try:
            path = {"products": "/public/products", "customers": "/customers",
                    "orders": "/orders"}.get(entity)
            if not path:
                return None
            authed = entity != "products"
            r = requests.get(self.base + path, params={"per_page": 1},
                             auth=self.auth if authed else None,
                             verify=self.verify, timeout=30)
            return _find_total(r.json())
        except Exception as exc:
            self.events.append(f"count {entity} failed: {exc}")
            return None

    def _variation_from_db(self) -> Optional[dict]:
        """Resolve one priced, active variation straight from the DB over
        WP-CLI. Returns {id, post_id, item_price} or None. Used because REST
        `/variants` OOMs at large catalogue sizes."""
        if not self.use_wpcli_products:
            return None
        try:
            cmd = (f"cd {self.wp_path} && {self.wp_cli} eval "
                   "'global $wpdb;$r=$wpdb->get_row(\"SELECT id,post_id,item_price "
                   "FROM {$wpdb->prefix}fct_product_variations WHERE item_price>0 "
                   "LIMIT 1\");if($r)echo $r->id.\",\".$r->post_id.\",\".$r->item_price;' "
                   "2>/dev/null")
            _, out, _ = self.ssh.run(cmd)
            line = out.strip().splitlines()[-1] if out.strip() else ""
            if not line or "," not in line:
                return None
            vid, pid, price = line.split(",")[:3]
            return {"id": int(vid), "post_id": int(pid), "item_price": int(price)}
        except Exception as exc:
            self.events.append(f"variation-from-db failed: {exc}")
            return None

    # ---- ensure a target volume -----------------------------------------
    def ensure(self, entity: str, target: int) -> dict:
        ceiling = CEILINGS.get(entity)
        if ceiling and target > ceiling:
            raise ValueError(f"target {target} for '{entity}' exceeds ceiling {ceiling}")
        current = self.current_count(entity) or 0
        delta = target - current
        if delta <= 0:
            return {"platform": "fluentcart", "entity": entity, "target": target,
                    "created": 0, "already": current, "method": "none"}
        if entity == "products":
            created = self._seed_products(delta)
        elif entity == "customers":
            created = self._seed_customers(delta)
        elif entity == "orders":
            created = self._seed_orders(delta)
        else:
            raise RuntimeError(f"unsupported entity {entity}")
        return {"platform": "fluentcart", "entity": entity, "target": target,
                "created": created, "already": current, "method": "rest",
                "events": list(self.events)}

    # ---- create single records ------------------------------------------
    def _create_product(self) -> bool:
        payload = {"post_title": f"Bench Product {uuid.uuid4().hex[:8]}",
                   "post_status": "publish",
                   "detail": {"fulfillment_type": "digital"}}
        return self._post_with_backoff("/products", payload)

    def _create_customer(self) -> bool:
        uid = uuid.uuid4().hex[:8]
        # current FC REST requires `full_name` (first/last are derived from it);
        # sending only first_name/last_name returns 422 "Full Name is required."
        payload = {"email": f"benchcust{uid}@example.com",
                   "full_name": "Bench Customer",
                   "first_name": "Bench", "last_name": "Customer",
                   "country": "US", "city": "Example", "state": "CA",
                   "postcode": "94102", "wp_user": "yes"}
        return self._post_with_backoff("/customers", payload)

    # ---- bulk seeding ----------------------------------------------------
    def _abort(self, entity: str, fails: int):
        raise RuntimeError(
            f"FC {entity} seeding aborted after {fails} consecutive failed attempts "
            f"({self.events[-1] if self.events else 'unknown error'})"
        )

    def _seed_products(self, delta: int) -> int:
        # PRICED products via FluentCart models over WP-CLI (so they can back
        # orders). Falls back to REST bulk-insert (bare, list-only) if no SSH.
        if self.use_wpcli_products:
            return self._seed_products_wpcli(delta)
        self.events.append("no SSH — falling back to REST bulk-insert; "
                           "these products are BARE (no priced variation) and "
                           "cannot back an order")
        return self._seed_products_rest(delta)

    def _seed_products_wpcli(self, delta: int) -> int:
        if not self._product_script_remote:
            remote = f"/tmp/bench_fc_products_{uuid.uuid4().hex[:6]}.php"
            self.ssh.put_text(FC_SEED_PRODUCT_PHP, remote)
            self._product_script_remote = remote
        chunk = self.throttle.entity_chunk.get("products", 200) or 200
        created = 0
        fails = 0
        while created < delta:
            n = min(chunk, delta - created)
            cmd = (f"cd {self.wp_path} && {self.wp_cli} eval-file "
                   f"{self._product_script_remote} {n} {self.default_price} 2>&1")
            code, out, err = self.ssh.run(cmd, timeout=600)
            tail = (out or "").strip().splitlines()[-1] if out.strip() else ""
            if code == 0 and tail.endswith("OK"):
                created += n
                fails = 0
            else:
                fails += 1
                self.events.append(f"FC product wp-cli chunk failed: {tail or err.strip()[:120]}")
                if fails >= self.max_failures:
                    self._abort("product", fails)
            time.sleep(self.throttle.chunk_pause_s)
        return created

    def _seed_products_rest(self, delta: int) -> int:
        # FC's /products/bulk-insert endpoint hard-caps at 10 items per call;
        # sending more returns 422 "Maximum 10 products per chunk allowed".
        FC_BULK_CAP = 10
        per_call = min(FC_BULK_CAP,
                       self.throttle.entity_chunk.get("products", FC_BULK_CAP)
                       or FC_BULK_CAP)
        bucket = TokenBucket(self.throttle.max_rps)
        created = 0
        fails = 0
        while created < delta:
            n = min(per_call, delta - created)
            payload = {"products": [
                {"post_title": f"Bench Product {uuid.uuid4().hex[:8]}",
                 "post_status": "publish",
                 "detail": {"fulfillment_type": "digital"}}
                for _ in range(n)
            ]}
            bucket.wait()
            if self._post_with_backoff("/products/bulk-insert", payload):
                created += n
                fails = 0
            else:
                fails += 1
                if fails >= self.max_failures:
                    self._abort("product", fails)
            time.sleep(self.throttle.chunk_pause_s)
        return created

    def _seed_customers(self, delta: int) -> int:
        # FC has no customer bulk-insert, so parallelize single creates with a
        # small worker pool (bounded — gentle on the API).
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(5, self.throttle.concurrency or 3))
        created = 0
        fails = 0
        while created < delta:
            batch = min(workers * 4, delta - created)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(lambda _: self._create_customer(), range(batch)))
            ok = sum(1 for r in results if r)
            created += ok
            if ok == 0:
                fails += 1
                if fails >= self.max_failures:
                    self._abort("customer", fails)
            else:
                fails = 0
        return created

    def _seed_orders(self, delta: int) -> int:
        refs = self.resolve_refs(create_if_missing=False)
        if not refs.get("customer_id") or not refs.get("variation_id"):
            raise RuntimeError("cannot seed FC orders: no customer/variation "
                               "(seed products + customers first)")
        bucket = TokenBucket(self.throttle.max_rps)
        created = 0
        fails = 0
        price = int(refs.get("unit_price") or 2500)
        while created < delta:
            payload = self.order_payload(refs, price)
            bucket.wait()
            if self._post_with_backoff("/orders", payload):
                created += 1
                fails = 0
            else:
                fails += 1
                if fails >= self.max_failures:
                    self._abort("order", fails)
            time.sleep(self.throttle.chunk_pause_s / 5)
        return created

    @staticmethod
    def order_payload(refs: dict, price: int) -> dict:
        return {
            "customer_id": int(refs["customer_id"]),
            "order_items": [{
                "post_id": int(refs["product_id"]),
                "variation_id": int(refs["variation_id"]),
                "object_id": int(refs["variation_id"]),
                "quantity": 1,
                "fulfillment_type": "digital", "payment_type": "onetime",
                "unit_price": price, "price": price, "item_cost": 0,
                "item_total": price, "tax_amount": 0, "discount_total": 0,
                "shipping_charge": 0, "total": price, "line_total": price,
                "post_title": "Bench Item", "title": "Bench Item",
            }],
            "fulfillment_type": "digital", "currency": "USD",
            "subtotal": price, "total_amount": price,
            # shipping_total MUST be present. FC OrderResource defaults a missing
            # key to [] -> cast to NULL on insert -> NOT NULL violation on
            # fct_orders.shipping_total -> uncaught 500 in createDraftOrder().
            "shipping_total": 0,
            "payment_method": "offline_payment",
        }

    # ---- reference-id resolution (self-sufficient) ----------------------
    def resolve_refs(self, create_if_missing: bool = True) -> dict:
        refs: dict = {}

        def grab_variation():
            # Prefer the DB when SSH is available: the REST `/variants` endpoint
            # loads the whole variation set into memory and OOMs (fatal error)
            # once the catalogue is large (100k+), so it can't be used to resolve
            # a single ref at benchmark scale. The DB read is O(1) and exact.
            v = self._variation_from_db()
            if not v:
                v = self._get_first("/variants") or self._get_first("/products/variants")
            if v:
                refs["variation_id"] = v.get("id")
                refs["product_id"] = v.get("post_id")
                refs["unit_price"] = v.get("item_price") or 2500

        grab_variation()
        if not refs.get("variation_id") and create_if_missing:
            self.events.append("no variation found — creating a product")
            if self._create_product():
                time.sleep(1.0)
                grab_variation()

        c = self._get_first("/customers")
        if not c and create_if_missing:
            self.events.append("no customer found — creating one")
            if self._create_customer():
                time.sleep(0.5)
                c = self._get_first("/customers")
        if c:
            refs["customer_id"] = c.get("id") or c.get("ID")
        return refs

    # ---- http w/ adaptive backoff ---------------------------------------
    def _post_with_backoff(self, path: str, payload: dict, attempt: int = 0) -> bool:
        try:
            r = requests.post(self.base + path, json=payload, auth=self.auth,
                              verify=self.verify, timeout=60)
        except requests.RequestException as exc:
            if attempt > 6:
                raise RuntimeError(f"FC {path} failed: {exc}")
            self._backoff(attempt, str(exc))
            return self._post_with_backoff(path, payload, attempt + 1)
        if r.status_code in (200, 201):
            return True
        if r.status_code in (429, 503):
            wait = 0.0
            try:
                wait = float(r.headers.get("Retry-After", 0))
            except (TypeError, ValueError):
                wait = 0.0
            self._backoff(attempt, f"HTTP {r.status_code}", wait)
            return self._post_with_backoff(path, payload, attempt + 1)
        # other 4xx/5xx: record and stop (don't infinite-loop)
        self.events.append(f"FC {path} -> {r.status_code}: {r.text[:160]}")
        return False

    def _backoff(self, attempt: int, reason: str, retry_after: float = 0.0) -> None:
        wait = retry_after or min(30.0, (2 ** attempt) * 0.5)
        self.throttle.max_rps = max(1.0, self.throttle.max_rps * 0.7)
        self.events.append(f"FC backoff {wait:.1f}s ({reason}); rps->{self.throttle.max_rps:.1f}")
        time.sleep(wait)


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #
def ensure_fc_order_refs(settings, platform_key: str = "fluentcart",
                         create_if_missing: bool = False) -> dict:
    """Resolve + persist FluentCart order-create reference IDs into .secrets so the
    order module injects a real customer_id / variation_id / price.

    By default this ONLY resolves — it assumes prerequisite products + customers
    were already seeded (the seed->measure flow, same rule as Woo/EDD). Pass
    create_if_missing=True to fall back to creating one of each (used only as a
    safety net)."""
    site = settings.sites.get(platform_key)
    if site is None:
        return {"ready": False, "reason": "no fluentcart site"}
    app_pw = settings.load_derived(platform_key).get("app_password") or site.app_password
    if not app_pw:
        return {"ready": False, "reason": "no app password (run bootstrap)"}
    # Build with SSH so variation resolution can read the DB directly — the REST
    # `/variants` endpoint OOMs at large catalogue sizes and can't resolve a ref.
    from .factory import make_platform_seeder
    seeder = make_platform_seeder(settings, platform_key) or \
        FluentCartSeeder(site.rest_url, site.admin_user, app_pw)
    try:
        refs = seeder.resolve_refs(create_if_missing=create_if_missing)
    finally:
        ssh = getattr(seeder, "ssh", None)
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass
    persist = {}
    if refs.get("customer_id"):
        persist["FC_CUSTOMER_ID"] = str(refs["customer_id"])
    if refs.get("product_id"):
        persist["FC_PRODUCT_ID"] = str(refs["product_id"])
    if refs.get("variation_id"):
        persist["FC_VARIATION_ID"] = str(refs["variation_id"])
    if refs.get("unit_price"):
        persist["FC_UNIT_PRICE"] = str(int(refs["unit_price"]))
    if persist:
        settings.save_derived(platform_key, persist)
    ready = bool(persist.get("FC_CUSTOMER_ID") and persist.get("FC_VARIATION_ID"))
    return {"ready": ready, "refs": persist, "events": seeder.events,
            "reason": None if ready else
            "no product+variation / customer found — seed products and customers "
            "first (they are the prerequisites for order placement)"}


def _first_list(data, depth: int = 0):
    """Return the first list-of-records found in a FluentCart response envelope,
    walking nested dicts (handles bare list, {data:[]}, {variants:[]},
    {products:{products:{data:[]}}}, etc.)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and depth < 6:
        for key in ("data", "variants", "products", "customers", "orders", "items"):
            if key in data:
                r = _first_list(data[key], depth + 1)
                if r:
                    return r
        for v in data.values():
            r = _first_list(v, depth + 1)
            if r:
                return r
    return []


def _find_total(data, depth: int = 0):
    """Find a 'total'/'total_count' integer anywhere in the response."""
    if isinstance(data, dict) and depth < 6:
        for k in ("total", "total_count", "found"):
            if isinstance(data.get(k), int):
                return data[k]
        for v in data.values():
            t = _find_total(v, depth + 1)
            if t is not None:
                return t
    return None
