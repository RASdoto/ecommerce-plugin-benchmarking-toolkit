"""WooCommerce order-create reference resolution.

The Woo order module posts a static line item; its product_id must point at a
real, purchasable product. A catalogue reset removes the original hardcoded id
(91), so we resolve one live product over the WC REST API and persist it to
.secrets as WOO_PRODUCT_ID for the module to inject.
"""
from __future__ import annotations

import requests


def ensure_woo_order_refs(settings, platform_key: str = "woo") -> dict:
    site = settings.sites.get(platform_key)
    if site is None:
        return {"ready": False, "reason": "no woo site"}
    sec = settings.secret_resolver(platform_key)
    ck = sec("WOO_CONSUMER_KEY", "")
    cs = sec("WOO_CONSUMER_SECRET", "")
    if not (ck and cs):
        return {"ready": False, "reason": "no woo consumer key/secret"}
    base = site.url.rstrip("/") + "/wp-json/wc/v3"
    try:
        r = requests.get(base + "/products",
                         params={"per_page": 1, "status": "publish",
                                 "consumer_key": ck, "consumer_secret": cs},
                         verify=False, timeout=30)
        items = r.json()
        pid = items[0].get("id") if isinstance(items, list) and items else None
    except Exception as exc:
        return {"ready": False, "reason": f"resolve failed: {exc}"}
    if not pid:
        return {"ready": False, "reason": "no published woo product found"}
    settings.save_derived(platform_key, {"WOO_PRODUCT_ID": str(pid)})
    return {"ready": True, "refs": {"WOO_PRODUCT_ID": str(pid)}}
