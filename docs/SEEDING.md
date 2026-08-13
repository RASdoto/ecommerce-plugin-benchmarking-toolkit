# How pre-benchmark data is created (seeding)

Before any operation is measured, each site must hold the right **prerequisite
data** — products, customers, and (for some operations) orders. This document
explains *exactly* how that data is created for each platform, why the methods
differ, and how the toolkit keeps the comparison fair.

> **Why this matters for accuracy.** A benchmark is only fair if every platform
> is populated through **its own native data model**, so that reads and writes
> hit the same tables a real store would use. FluentCart, WooCommerce, and Easy
> Digital Downloads (EDD) store commerce data very differently, so their seeders
> are necessarily different. What is held constant is the *target state* (N
> products / N customers / N orders), the *volumes*, and the *measurement*, not
> the insertion mechanism.

---

## 1. The seed → measure contract (identical for all platforms)

Seeding is orchestrated by [`bench/seed/factory.py`](../bench/seed/factory.py)
and is governed by one rule set, applied uniformly:

| Operation being measured | What seeding does |
|---|---|
| `product-list` / `search` | Ensure exactly `volume` products exist. **Idempotent top-up** — if the site already has ≥ `volume`, it does nothing. For clean per-tier reads, the catalogue is reset to zero and grown **ascending** (0 → 1k → 10k → 50k → 100k) so each tier is measured with precisely that many rows present. |
| `order-list` | Verify prerequisite products **and** customers exist, then ensure `volume` orders. |
| `order-create` / `checkout` | **Verify prerequisites only — never create them.** Creating products/customers here would pollute the volume counts other cells depend on. If prerequisites are missing, the cell is *skipped*, not measured against a broken store. |
| `product-create` / `customer-create` | Nothing to pre-seed — the operation *is* the creation. |

Shared guarantees:

- **Idempotent & additive.** Seeding always seeds *up to* a level and never
  wipes. Re-running is safe.
- **Hard ceilings.** Products ≤ 100,000; orders and customers ≤ 5,000
  (configurable in [`matrix.yaml`](../matrix.yaml)). A seed request above the
  ceiling is refused, not silently truncated.
- **Rate limited.** Seeding is throttled (`seed_max_rps`, batch sizes, inter-batch
  pauses) so it never itself becomes the thing under test.
- **Prerequisite constants.** `prereq_products` (default 10) and
  `prereq_customers` (default 5) are the minimum a store needs before an order
  can be placed.

Everything below is about *how each platform reaches that target state*.

---

## 2. FluentCart — custom relational tables (`fct_*`)

Seeder: [`bench/seed/fluentcart.py`](../bench/seed/fluentcart.py). FluentCart
stores products, variations, customers, and orders in **purpose-built tables**
(`fct_product_details`, `fct_product_variations`, `fct_customers`, `fct_orders`).

| Asset | How it is created | Counted via |
|---|---|---|
| **Products** | Preferred path (needs SSH + WP-CLI): a `fluent-products` post is inserted, then a **priced `ProductVariation`** (`item_price` in cents, active, one-time) and a `ProductDetail` (`min/max_price`, `default_variation_id`) are written through FluentCart's own models. This produces a product that can actually back an order. Fallback (REST only): `POST /products/bulk-insert` creates **bare, list-only** products that cannot be ordered — logged as a fallback. | DB count on `fct_product_details` |
| **Customers** | REST `POST /fluent-cart/v2/customers` with `full_name`, email, and address fields. (The current REST contract requires `full_name`.) | DB count on `fct_customers` |
| **Orders** | REST `POST /fluent-cart/v2/orders` (admin create — stateless, good for concurrent load) with a resolved `customer_id`, `post_id`/`variation_id`, and reconciled money fields. The variation reference is read **directly from the database** (`fct_product_variations`) rather than the REST `/variants` endpoint, which exhausts PHP memory once the catalogue is large. | DB count on `fct_orders` |

**Why the priced-variation dance?** FluentCart's public REST cannot persist a
variation *price*, so a REST-only product returns an empty `/variants` list and
throws when you try to order it. Creating the variation through the model over
WP-CLI is the only reliable way to seed an orderable product — so FluentCart
product seeding **requires SSH + WP-CLI**. Without it, the toolkit degrades to
bare products and records that order-create cannot be measured.

---

## 3. WooCommerce — `wp_posts` products + custom order tables

Seeder: [`bench/seed/seeder.py`](../bench/seed/seeder.py) (`SEED_SCRIPTS["woo"]`),
run server-side over WP-CLI.

| Asset | How it is created | Counted via |
|---|---|---|
| **Products** | **Bulk multi-row `INSERT`** into `wp_posts` (`post_type=product`), `wp_postmeta` (`_price`, `_regular_price`, `_stock_status`, `_manage_stock`, `_sku`), and `wp_term_relationships` (the `simple` `product_type` term). This mirrors exactly what WooCommerce writes, at ~1,000–5,000 products/sec. | `wp post list --post_type=product` |
| **Customers** | WP-CLI `wc_create_new_customer(...)`, then the meta WooCommerce needs to *recognise* a customer: `paying_customer`, first/last name, and full billing + shipping address. | `wp user list --role=customer` |
| **Orders** | WP-CLI `wc_create_order()` → `add_product()` (random product) → `set_address()` → `calculate_totals()` → `update_status('completed')`. | DB count on the WooCommerce orders table |

> **Important operational note for WooCommerce.** Because products are inserted
> with bulk SQL rather than through `WC_Product_Simple->save()`, WooCommerce's
> `wc_product_meta_lookup` table is **not** populated automatically. After a bulk
> product seed you must run
> `wp wc tool run regenerate_product_lookup_tables --user=1`
> and let Action Scheduler drain **before measuring**, otherwise WooCommerce runs
> a background regeneration *during* the benchmark and its write numbers are
> inflated. The older per-product `WC_Product_Simple->save()` path avoids this but
> tops out around ~4 products/sec, which makes 100k tiers impractical and can drop
> a long SSH connection mid-seed. Choose speed + a lookup regen, or fidelity + a
> long seed — the toolkit defaults to the fast path and documents the regen step.

---

## 4. Easy Digital Downloads (EDD) — `download` posts + custom order tables

Seeder: [`bench/seed/seeder.py`](../bench/seed/seeder.py) (`SEED_SCRIPTS["edd"]`),
run server-side over WP-CLI.

| Asset | How it is created | Counted via |
|---|---|---|
| **Products (downloads)** | WP-CLI `wp_insert_post(post_type=download)` + an `edd_price` meta value. EDD's product model is light, so this path stays fast without bulk SQL. | `wp post list --post_type=download` |
| **Customers** | WP-CLI `edd_add_customer(['email','name'])`. | DB count on `edd_customers` |
| **Orders** | WP-CLI `edd_insert_payment(...)` with `user_info`, `downloads[]`, and `cart_details[]` (purchase key, currency, status = complete). | DB count on `edd_orders` |

---

## 5. Side-by-side summary

| | FluentCart | WooCommerce | EDD |
|---|---|---|---|
| Product storage | custom `fct_*` tables | `wp_posts` + `wp_postmeta` (+ lookup) | `wp_posts` (`download`) + `wp_postmeta` |
| Product seed method | WP-CLI via models (priced variation) / REST fallback | WP-CLI bulk SQL insert | WP-CLI `wp_insert_post` |
| Customer seed method | REST `POST /customers` | WP-CLI `wc_create_new_customer` | WP-CLI `edd_add_customer` |
| Order seed method | REST `POST /orders` (admin) | WP-CLI `wc_create_order` | WP-CLI `edd_insert_payment` |
| Product count source | DB `fct_product_details` | `wp post list` | `wp post list` |
| Needs SSH/WP-CLI to seed orderable products? | **Yes** (priced variation) | Yes | Yes |
| Post-seed maintenance | none | **regenerate product lookup table** | none |

**The through-line:** every platform is filled using the same creation calls a
real store on that platform would use, so the measured reads and writes exercise
each platform's genuine schema. The differences you see in the seeders are the
differences in the platforms themselves — which is precisely what the benchmark
sets out to measure.

---

## 6. Reproducing a clean run

1. `bench bootstrap` provisions WP-CLI + the profiler and mints credentials.
2. `bench seed --all` (or per-cell auto-seed during `bench run-all`) brings each
   site to the required volumes using the methods above.
3. For WooCommerce specifically, after any bulk product seed, regenerate the
   product lookup table (see §3) before measuring.
4. `bench run-all` measures; seeding is verified (not re-created) for order cells.

See [AGENT_RUNBOOK.md](AGENT_RUNBOOK.md) for the full unattended flow.
