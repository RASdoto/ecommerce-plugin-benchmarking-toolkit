"""10-request smoke test across {fluentcart, woo, edd} x {product-list, product-create, order-create}.

Uses bench.run.runner.Runner.run_cell to exercise the real seed->measure path.
Volume=10, concurrency=1 keeps max_requests at exactly 10 per cell.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench.settings import Settings
from bench.run.runner import Runner

s = Settings(root=".")
r = Runner(settings=s)

platforms = ["fluentcart", "woo", "edd"]
ops = ["product-list", "product-create", "order-create"]

def cell(platform, op):
    entity = {"product-list": "products", "product-create": "products",
              "order-create": "orders"}[op]
    return {"platform": platform, "operation": op, "entity": entity,
            "volume": 10, "concurrency": 1}

rows = []
t0 = time.time()
for p in platforms:
    for o in ops:
        c = cell(p, o)
        t_cell = time.time()
        print(f"\n=== [{time.strftime('%H:%M:%S')}] {p:11s} {o:15s} volume={c['volume']} conc={c['concurrency']} ===", flush=True)
        try:
            row = r.run_cell(c)
        except Exception as exc:
            row = dict(c)
            row["status"] = f"exception: {exc}"
        dt = time.time() - t_cell
        req = row.get("total_requests", 0)
        err = row.get("total_errors", 0)
        avg = row.get("average_ms")
        codes = row.get("error_codes")
        avg_str = f"{round(avg, 1)}" if avg else "-"
        print(f"  status={row.get('status')} reqs={req} errors={err} avg_ms={avg_str} codes={codes} took={dt:.1f}s", flush=True)
        rows.append(row)
        with open("reports/results.jsonl", "a") as f:
            f.write(json.dumps(row, default=str) + "\n")

print(f"\n=== SUMMARY ({time.time()-t0:.1f}s total) ===", flush=True)
hdr = f"{'platform/op':<28} {'reqs':>5} {'err':>5} {'avg_ms':>8} {'codes':<20} {'status':<15}"
print(hdr)
print("-" * len(hdr))
for row in rows:
    key = f"{row.get('platform')}/{row.get('operation')}"
    avg = row.get("average_ms")
    avg_str = f"{round(avg,1)}" if avg else "-"
    codes = str(row.get("error_codes") or {})[:20]
    print(f"{key:<28} {row.get('total_requests',0):>5} {row.get('total_errors',0):>5} "
          f"{avg_str:>8} {codes:<20} {str(row.get('status',''))[:15]:<15}")
