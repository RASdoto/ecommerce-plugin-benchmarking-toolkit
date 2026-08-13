"""
Rate-limited, convention-aware data seeder (Phase 5, [CLI/LT]).

Design informed by the manual EDD/Woo seeding scripts (see
SEEDER_WOO_EDD_CONVENTIONS.md). Key principles adopted:

  * SERVER-SIDE FIRST: build volume with WP-CLI (`wp eval-file`) looping inside
    the server — matching the manual plugins — instead of hammering public REST
    per row. Throttled client-REST is a fallback only when SSH is unavailable.
  * CHUNK + PAUSE: run in server-side chunks with an inter-chunk sleep so the
    server is never overwhelmed (the manual scripts sleep every N heavy items).
  * DEPENDENCY ORDER: products -> customers (Woo) -> orders; orders reference
    REAL existing IDs fetched from the site, never hardcoded ones.
  * CORRECT SCHEMAS: Woo customers get the billing/shipping meta + paying_customer
    + wc_last_active they need to be valid; EDD payments use the full
    edd_insert_payment schema (purchase_key, downloads[], cart_details[]).
  * UNIQUE identity fields (emails, purchase keys, SKUs, titles) via uniqid().
  * IMAGES OMITTED by design (dominant per-item cost; irrelevant to DB benchmark).
  * ERROR ISOLATION: per-chunk failures are tolerated up to a threshold and
    recorded, never aborting a whole tier on one bad row.
  * IDEMPOTENT top-up + hard entity ceilings (products<=100k, orders/customers<=5k).

The throttle/chunking here is SEED-ONLY and never touches the measured load runs.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..sshutil import SSHClient

CEILINGS = {"products": 100_000, "orders": 5_000, "customers": 5_000}

# minimum prerequisites that must exist before placing orders. Small by default
# so a 1-request smoke cell isn't forced to seed a large catalogue first;
# overridable per run via matrix.yaml (prereq_products / prereq_customers).
PREREQ = {"products": 10, "customers": 5}


@dataclass
class SeedThrottle:
    max_rps: float = 10.0            # client-REST fallback rate cap
    concurrency: int = 3            # client-REST fallback parallelism (kept low)
    # server-side chunking (WP-CLI path)
    chunk_size: int = 2000          # rows per `wp eval-file` call (matches manual 2k chunks)
    chunk_pause_s: float = 1.0      # sleep between chunks
    # per-entity chunk overrides (heavier ops -> smaller chunks)
    entity_chunk: dict = field(default_factory=lambda: {
        "products": 2000, "customers": 1000, "orders": 500,
    })
    failure_tolerance: float = 0.05  # abort tier only if >5% of chunks fail
    wpcli_switch_products: int = 50_000  # (kept for compatibility; WP-CLI is default now)


class TokenBucket:
    def __init__(self, rate: float):
        self.rate = max(0.1, rate)
        self.allowance = self.rate
        self.last = time.monotonic()

    def wait(self) -> None:
        now = time.monotonic()
        self.allowance += (now - self.last) * self.rate
        self.last = now
        if self.allowance > self.rate:
            self.allowance = self.rate
        if self.allowance < 1.0:
            time.sleep((1.0 - self.allowance) / self.rate)
            self.allowance = 0.0
        else:
            self.allowance -= 1.0


# --------------------------------------------------------------------------- #
# Server-side generator snippets (run via `wp eval-file <file> <count>`).
# Each loops `$args[0]` times inside the server. Images intentionally omitted.
# --------------------------------------------------------------------------- #
SEED_SCRIPTS: dict[str, dict[str, str]] = {
    "edd": {
        "products": r"""<?php
$count = isset($args[0]) ? (int)$args[0] : 0;
for ($i=0;$i<$count;$i++){
  $id = wp_insert_post(['post_title'=>'Bench Download '.uniqid(),'post_content'=>'bench',
    'post_type'=>'download','post_status'=>'publish','post_author'=>1]);
  if (!is_wp_error($id)) { update_post_meta($id,'edd_price', rand(10,100)); }
}
echo "OK";
""",
        "customers": r"""<?php
$count = isset($args[0]) ? (int)$args[0] : 0;
if (!function_exists('edd_add_customer')) { echo "NO_EDD"; return; }
for ($i=0;$i<$count;$i++){
  $email='benchcust'.uniqid().'@example.com';
  edd_add_customer(['email'=>$email,'name'=>'Bench Customer']);
}
echo "OK";
""",
        "orders": r"""<?php
$count = isset($args[0]) ? (int)$args[0] : 0;
$dl = get_posts(['post_type'=>'download','posts_per_page'=>200,'fields'=>'ids','post_status'=>'publish']);
if (empty($dl)) { echo "NO_PREREQ_DOWNLOADS"; return; }
if (!function_exists('edd_insert_payment')) { echo "NO_EDD"; return; }
for ($i=0;$i<$count;$i++){
  $did = $dl[array_rand($dl)];
  $price = rand(20,200);
  edd_insert_payment([
    'price'=>$price,'date'=>date('Y-m-d H:i:s'),
    'user_email'=>'bench'.uniqid().'@example.com',
    'purchase_key'=>strtolower(md5(uniqid('',true))),
    'currency'=>'USD',
    'downloads'=>[['id'=>$did]],
    'cart_details'=>[['name'=>get_the_title($did),'id'=>$did,'item_price'=>$price,
      'price'=>$price,'quantity'=>1,'subtotal'=>$price,'tax'=>0,'discount'=>0,'total'=>$price]],
    'status'=>'complete',
  ]);
}
echo "OK";
""",
    },
    "woo": {
        # Bulk multi-row INSERT straight into posts/postmeta/term_relationships.
        # The WC_Product_Simple->save() path managed only ~4 products/sec, so a
        # 100k tier took hours over one long-lived SSH eval and dropped the
        # connection mid-seed (leaving a partial catalogue). This does ~1-5k/sec
        # and completes each chunk in seconds. Products carry _price /
        # _regular_price / stock meta + the `simple` product_type term, so they
        # list via WC REST and are returned by wc_get_products.
        "products": r"""<?php
$count = isset($args[0]) ? (int)$args[0] : 0;
global $wpdb;
$now = current_time('mysql'); $gmt = current_time('mysql', 1);
$tt = $wpdb->get_var("SELECT tt.term_taxonomy_id FROM {$wpdb->prefix}term_taxonomy tt JOIN {$wpdb->prefix}terms t ON t.term_id=tt.term_id WHERE tt.taxonomy='product_type' AND t.slug='simple' LIMIT 1");
$B = 500;
for ($off=0; $off<$count; $off+=$B) {
  $k = min($B, $count-$off); $vals=[];
  for ($i=0;$i<$k;$i++){ $vals[] = $wpdb->prepare("(1,%s,%s,%s,'','publish',%s,%s,%s,'product')", $now,$gmt,'Bench Product '.uniqid(),'bench-'.uniqid(),$now,$gmt); }
  $wpdb->query("INSERT INTO {$wpdb->prefix}posts (post_author,post_date,post_date_gmt,post_title,post_content,post_status,post_name,post_modified,post_modified_gmt,post_type) VALUES ".implode(',',$vals));
  $first=$wpdb->insert_id; $pm=[]; $tr=[];
  for ($i=0;$i<$k;$i++){ $pid=$first+$i; $pr=rand(10,100);
    $pm[]=$wpdb->prepare("(%d,'_price',%s)",$pid,$pr);
    $pm[]=$wpdb->prepare("(%d,'_regular_price',%s)",$pid,$pr);
    $pm[]=$wpdb->prepare("(%d,'_stock_status','instock')",$pid);
    $pm[]=$wpdb->prepare("(%d,'_manage_stock','no')",$pid);
    $pm[]=$wpdb->prepare("(%d,'_sku',%s)",$pid,'BENCH-'.$pid);
    if($tt) $tr[]=$wpdb->prepare("(%d,%d,0)",$pid,$tt);
  }
  $wpdb->query("INSERT INTO {$wpdb->prefix}postmeta (post_id,meta_key,meta_value) VALUES ".implode(',',$pm));
  if($tr) $wpdb->query("INSERT INTO {$wpdb->prefix}term_relationships (object_id,term_taxonomy_id,term_order) VALUES ".implode(',',$tr));
}
echo "OK";
""",
        "customers": r"""<?php
$count = isset($args[0]) ? (int)$args[0] : 0;
if (!function_exists('wc_create_new_customer')) { echo "NO_WC"; return; }
for ($i=0;$i<$count;$i++){
  $email='benchcust'.uniqid().'@example.com';
  $uid = wc_create_new_customer($email,'benchcust'.uniqid(),wp_generate_password());
  if (!is_wp_error($uid)) {
    // meta required for WooCommerce to recognise + report a customer
    update_user_meta($uid,'paying_customer',1);
    update_user_meta($uid,'first_name','Bench');
    update_user_meta($uid,'last_name','Customer');
    foreach(['billing','shipping'] as $t){
      update_user_meta($uid,$t.'_first_name','Bench');
      update_user_meta($uid,$t.'_last_name','Customer');
      update_user_meta($uid,$t.'_address_1',rand(100,999).' Main St');
      update_user_meta($uid,$t.'_city','Example City');
      update_user_meta($uid,$t.'_state','CA');
      update_user_meta($uid,$t.'_postcode',(string)rand(10000,99999));
      update_user_meta($uid,$t.'_country','US');
    }
    update_user_meta($uid,'billing_email',$email);
    update_user_meta($uid,'billing_phone','555-555-'.rand(1000,9999));
    update_user_meta($uid,'wc_last_active',time());
  }
}
echo "OK";
""",
        "orders": r"""<?php
$count = isset($args[0]) ? (int)$args[0] : 0;
if (!function_exists('wc_create_order')) { echo "NO_WC"; return; }
$prods = wc_get_products(['limit'=>200,'return'=>'ids','status'=>'publish']);
if (empty($prods)) { echo "NO_PREREQ_PRODUCTS"; return; }
$custs = get_users(['role'=>'customer','number'=>200,'fields'=>'ID']);
for ($i=0;$i<$count;$i++){
  $pid = $prods[array_rand($prods)];
  $cid = !empty($custs) ? $custs[array_rand($custs)] : 0;
  $order = wc_create_order(['customer_id'=>$cid]);
  $prod = wc_get_product($pid);
  if ($prod) { $order->add_product($prod, rand(1,3)); }
  $order->set_address([
    'first_name'=>'Bench','last_name'=>'Buyer',
    'email'=>'benchorder'.uniqid().'@example.com','phone'=>'5555555555',
    'address_1'=>'123 Bench St','city'=>'Example','state'=>'CA',
    'postcode'=>'90001','country'=>'US',
  ],'billing');
  $order->calculate_totals();
  $order->update_status('completed');
}
echo "OK";
""",
    },
}

# candidate count commands (tried in order; first non-empty numeric wins)
COUNT_CMDS: dict[str, dict[str, list[str]]] = {
    "edd": {
        "products": ["post list --post_type=download --format=count --post_status=publish"],
        "customers": ["eval 'global $wpdb;echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM {$wpdb->prefix}edd_customers\");'"],
        "orders": [
            "eval 'global $wpdb;echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM {$wpdb->prefix}edd_orders\");'",
            "post list --post_type=edd_payment --format=count",
        ],
    },
    "woo": {
        "products": ["post list --post_type=product --format=count --post_status=publish"],
        "customers": ["user list --role=customer --format=count"],
        "orders": [
            "eval 'global $wpdb;echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM {$wpdb->prefix}wc_orders\");'",
            "post list --post_type=shop_order --format=count",
        ],
    },
    "fluentcart": {
        # FluentCart stores products as posts + a details row; count via fct_product_details.
        "products": ["eval 'global $wpdb;echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM {$wpdb->prefix}fct_product_details\");'"],
        "orders": ["eval 'global $wpdb;echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM {$wpdb->prefix}fct_orders\");'"],
        "customers": ["eval 'global $wpdb;echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM {$wpdb->prefix}fct_customers\");'"],
    },
}


class PlatformSeeder:
    """Seeds one platform. Uses WP-CLI server-side when SSH is available,
    else falls back to throttled client-REST creators supplied by the caller."""

    def __init__(self, platform: str, site, throttle: SeedThrottle,
                 ssh: Optional[SSHClient] = None, wp_path: str = "", wp_cli: str = "wp"):
        self.platform = platform
        self.site = site
        self.throttle = throttle
        self.ssh = ssh
        self.wp_path = wp_path
        self.wp = wp_cli
        self.events: list[str] = []
        self._script_paths: dict[str, str] = {}

    @property
    def use_wpcli(self) -> bool:
        return self.ssh is not None and bool(self.wp_path)

    # ---- counts ----------------------------------------------------------
    def current_count(self, entity: str) -> Optional[int]:
        if not self.use_wpcli:
            return None
        for tail in COUNT_CMDS.get(self.platform, {}).get(entity, []):
            code, out, _ = self.ssh.run(f"cd {self.wp_path} && {self.wp} {tail} 2>/dev/null")
            val = out.strip().splitlines()[-1] if out.strip() else ""
            try:
                return int(val)
            except ValueError:
                continue
        return None

    # ---- public: ensure a target volume ---------------------------------
    def ensure(self, entity: str, target: int,
               rest_create: Optional[Callable[[int], object]] = None) -> dict:
        ceiling = CEILINGS.get(entity)
        if ceiling and target > ceiling:
            raise ValueError(f"target {target} for '{entity}' exceeds ceiling {ceiling}")

        # dependency ordering: orders need prerequisite products (+ customers for woo)
        if entity == "orders":
            self._ensure_prereqs()

        current = self.current_count(entity) or 0
        delta = target - current
        if delta <= 0:
            return {"platform": self.platform, "entity": entity, "target": target,
                    "created": 0, "already": current, "method": "none"}

        if self.use_wpcli:
            created = self._seed_wpcli(entity, delta)
            method = "wp-cli"
        elif rest_create is not None:
            created = self._seed_rest(rest_create, delta)
            method = "rest"
        else:
            raise RuntimeError(f"no seed path for {self.platform}/{entity} "
                               f"(no SSH/WP-CLI and no REST creator)")

        return {"platform": self.platform, "entity": entity, "target": target,
                "created": created, "already": current, "method": method,
                "events": list(self.events)}

    def _ensure_prereqs(self) -> None:
        # products first, then customers (Woo)
        if (self.current_count("products") or 0) < PREREQ["products"]:
            self.ensure("products", PREREQ["products"])
        if self.platform == "woo" and (self.current_count("customers") or 0) < PREREQ["customers"]:
            self.ensure("customers", PREREQ["customers"])

    # ---- WP-CLI server-side chunked path ---------------------------------
    def _script_for(self, entity: str) -> Optional[str]:
        php = SEED_SCRIPTS.get(self.platform, {}).get(entity)
        if php is None:
            return None
        if entity in self._script_paths:
            return self._script_paths[entity]
        remote = f"/tmp/bench_seed_{self.platform}_{entity}_{uuid.uuid4().hex[:6]}.php"
        self.ssh.put_text(php, remote)
        self._script_paths[entity] = remote
        return remote

    def _seed_wpcli(self, entity: str, delta: int) -> int:
        remote = self._script_for(entity)
        if remote is None:
            raise RuntimeError(f"no server-side script for {self.platform}/{entity}")
        chunk = self.throttle.entity_chunk.get(entity, self.throttle.chunk_size)
        created = 0
        chunks = 0
        failed = 0
        while created < delta:
            n = min(chunk, delta - created)
            cmd = f"cd {self.wp_path} && {self.wp} eval-file {remote} {n} 2>&1"
            code, out, err = self.ssh.run(cmd, timeout=600)
            chunks += 1
            tail = (out or "").strip().splitlines()[-1] if out.strip() else ""
            if code != 0 or tail.startswith("NO_") or "PREREQ" in tail:
                failed += 1
                self.events.append(f"{entity} chunk failed: {tail or err.strip()[:80]}")
                if failed / max(1, chunks) > self.throttle.failure_tolerance and chunks >= 3:
                    raise RuntimeError(f"{self.platform}/{entity} seeding aborted: "
                                       f"failure rate exceeded ({failed}/{chunks})")
            else:
                created += n
            time.sleep(self.throttle.chunk_pause_s)
        return created

    # ---- throttled client-REST fallback ---------------------------------
    def _seed_rest(self, rest_create: Callable[[int], object], delta: int) -> int:
        bucket = TokenBucket(self.throttle.max_rps)
        created = 0
        failed = 0
        while created < delta:
            n = min(self.throttle.entity_chunk.get("orders", 500), delta - created)
            for _ in range(n):
                bucket.wait()
                try:
                    resp = rest_create(1)
                    status = getattr(resp, "status_code", 200)
                    if status in (429, 503):
                        self._rest_backoff(resp)
                        continue
                    if status >= 500:
                        failed += 1
                    else:
                        created += 1
                except Exception as exc:
                    failed += 1
                    self.events.append(f"rest create error: {exc}")
                if created and failed / (created + failed) > self.throttle.failure_tolerance \
                        and (created + failed) > 20:
                    raise RuntimeError("REST seeding aborted: failure rate exceeded")
            time.sleep(self.throttle.chunk_pause_s)
        return created

    def _rest_backoff(self, resp) -> None:
        try:
            wait = float(resp.headers.get("Retry-After", 0)) or 2.0
        except (TypeError, ValueError):
            wait = 2.0
        self.throttle.max_rps = max(1.0, self.throttle.max_rps * 0.7)
        self.events.append(f"rest backoff {wait:.1f}s; rps->{self.throttle.max_rps:.1f}")
        time.sleep(wait)

    # ---- optional chunked reset (never synchronous bulk delete) ----------
    def reset(self, entity: str, chunk: int = 2000) -> dict:
        """Chunked WP-CLI deletion (mirrors the manual cron-chunk cleanup)."""
        if not self.use_wpcli:
            return {"reset": "skipped (no wp-cli)"}
        post_type = {"products": "product" if self.platform == "woo" else "download",
                     "orders": "shop_order" if self.platform == "woo" else "edd_payment"}.get(entity)
        if not post_type:
            return {"reset": "unsupported"}
        removed = 0
        while True:
            cmd = (f"cd {self.wp_path} && {self.wp} post list --post_type={post_type} "
                   f"--format=ids --posts_per_page={chunk} 2>/dev/null")
            _, ids, _ = self.ssh.run(cmd)
            ids = ids.strip()
            if not ids:
                break
            self.ssh.run(f"cd {self.wp_path} && {self.wp} post delete {ids} --force 2>/dev/null",
                         timeout=600)
            removed += len(ids.split())
            time.sleep(self.throttle.chunk_pause_s)
        return {"reset": entity, "removed": removed}
