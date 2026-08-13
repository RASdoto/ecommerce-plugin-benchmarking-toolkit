"""
Seeder factory + the seed->measure ordering rule, applied UNIFORMLY to all
platforms (FluentCart, Woo, EDD).

The dependency is the same everywhere: a store only accepts an order once valid
products and customers exist. Rules applied by `prepare_platform_data`:

  product-list / search   -> ensure `volume` products (skips seed call if
                             current count already >= volume)
  order-list              -> verify prereq products + customers exist, then
                             ensure `volume` orders
  order-create / checkout -> VERIFY prereqs only; NEVER auto-create products
                             or customers here (that would pollute the
                             100/1000/10000 matrix counts). Cell is skipped
                             by the runner if prereqs are missing.
  product-create          -> nothing to prepare
  customer-create         -> nothing to prepare

FluentCart seeds via its REST API (bulk-insert / create); Woo & EDD seed via
WP-CLI over SSH. Both expose the same `seeder.ensure(entity, target)` call.
"""
from __future__ import annotations

from typing import Optional

from .seeder import PlatformSeeder, SeedThrottle, PREREQ
from .fluentcart import FluentCartSeeder
from ..sshutil import from_site


def make_platform_seeder(settings, platform: str):
    """Return a seeder with a uniform `.ensure(entity, target)` API, or None."""
    site = settings.sites.get(platform)
    if site is None:
        return None
    if platform == "fluentcart":
        derived = settings.load_derived(platform)
        app_pw = derived.get("app_password") or site.app_password
        if not app_pw:
            return None
        # SSH lets FC create PRICED products via its models (REST can't). Optional
        # — without it, product seeding falls back to bare REST products.
        ssh = None
        if site.ssh_host and (site.ssh_key or site.ssh_pass):
            try:
                ssh = from_site(site)
                ssh.connect()
            except Exception:
                ssh = None
        return FluentCartSeeder(site.rest_url, site.admin_user, app_pw,
                                ssh=ssh, wp_path=derived.get("wp_path", ""),
                                wp_cli=derived.get("wp_cli", "wp"))
    # Woo / EDD: WP-CLI over SSH
    derived = settings.load_derived(platform)
    wp_path = derived.get("wp_path", "")
    wp_cli = derived.get("wp_cli", "wp")
    ssh = None
    if site.ssh_host and (site.ssh_key or site.ssh_pass):
        try:
            ssh = from_site(site)
            ssh.connect()
        except Exception:
            ssh = None
    return PlatformSeeder(platform, site, SeedThrottle(), ssh=ssh,
                          wp_path=wp_path, wp_cli=wp_cli)


def prepare_platform_data(settings, platform: str, operation: str,
                          volume: int) -> dict:
    """Ensure prerequisites (or target volume) exist for a measured cell.

    Rules:
      * `product-create` / `customer-create`: nothing to prepare (fast exit,
        no SSH open).
      * `product-list` / `search`: ensure `volume` of the target entity exists.
        Idempotent — a check-only pass when current count already >= volume.
      * `order-list`: verify prereq products + customers exist, then ensure
        `volume` orders.
      * `order-create` / `checkout`: **verify only**. NEVER auto-create
        products/customers here — doing so would pollute the 100/1000/10000
        matrix counts for the read/create cells that follow. If prereqs are
        missing, returns `seeded: False` with a clear reason so the caller
        can skip the cell.

    Never raises — returns a summary the caller can log.
    """
    # ------------------------------------------------------------------
    # Fast path: nothing to pre-seed / verify — don't even open SSH.
    # ------------------------------------------------------------------
    if operation in ("product-create", "customer-create"):
        return {"seeded": True, "skipped": True,
                "reason": "no prep required for create ops",
                "steps": []}

    seeder = make_platform_seeder(settings, platform)
    if seeder is None:
        return {"seeded": False, "reason": "no seeder (missing creds/ssh)",
                "steps": []}

    mx = getattr(settings, "matrix", {}) or {}
    pq = int(mx.get("prereq_products", PREREQ["products"]))
    cq = int(mx.get("prereq_customers", PREREQ["customers"]))
    results: list = []
    ok = True
    err: Optional[str] = None

    def _verify(entity: str, need: int) -> bool:
        """Count-only check. Returns True if satisfied; records a step either
        way. Never creates."""
        have = seeder.current_count(entity)
        step = {"entity": entity, "have": have, "need": need}
        if have is None:
            step.update(seeded=False, reason="count unavailable")
            results.append(step)
            return False
        if have >= need:
            step.update(skipped=True, reason="already satisfied")
            results.append(step)
            return True
        step.update(seeded=False, reason="missing prereq")
        results.append(step)
        return False

    try:
        if operation in ("product-list", "search"):
            # ensure() is already idempotent (returns delta=0 no-op when
            # current >= target), but skip the whole call when we can — that
            # avoids the WP-CLI seed-scripts upload / prereq recursion path.
            have = seeder.current_count("products")
            if have is not None and have >= volume:
                results.append({"entity": "products", "have": have,
                                "need": volume, "skipped": True,
                                "reason": "already satisfied"})
            else:
                results.append(seeder.ensure("products", volume))

        elif operation == "order-list":
            # verify prereqs (do NOT create — matrix counts must stay clean)
            if not _verify("products", pq):
                return {"seeded": False,
                        "error": f"missing prereq products (have "
                                 f"{results[-1].get('have')}, need {pq})",
                        "steps": results}
            if platform == "woo" and not _verify("customers", cq):
                return {"seeded": False,
                        "error": f"missing prereq customers (have "
                                 f"{results[-1].get('have')}, need {cq})",
                        "steps": results}
            # orders themselves: ensure target volume (this is the "read data"
            # for the list op, not a prereq, so seeding here is the whole point)
            results.append(seeder.ensure("orders", volume))

        elif operation in ("order-create", "checkout"):
            # VERIFY ONLY. Creating products/customers here would muddy the
            # matrix's 100/1000/10000 product/customer counts for other cells.
            if not _verify("products", pq):
                return {"seeded": False,
                        "error": f"missing prereq products for order-create "
                                 f"(have {results[-1].get('have')}, need {pq}) "
                                 f"— seed the product tier first",
                        "steps": results}
            # customers: Woo/EDD prereq is a user-role count; FC uses its own
            # fct_customers table (covered by current_count("customers")).
            if not _verify("customers", cq):
                return {"seeded": False,
                        "error": f"missing prereq customers for order-create "
                                 f"(have {results[-1].get('have')}, need {cq}) "
                                 f"— seed the customer tier first",
                        "steps": results}
    except Exception as exc:
        ok = False
        err = str(exc)
    finally:
        ssh = getattr(seeder, "ssh", None)
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass
    return {"seeded": ok, "error": err, "steps": results}
