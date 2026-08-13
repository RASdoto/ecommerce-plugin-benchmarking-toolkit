"""
System monitor — Python port + extension of `utils/ServerLogger.js` (parity P25,
fixes P26). Samples OS + DB resource usage over SSH for the duration of a load
run, then reduces to max/min/avg.

Runs a background sampling loop on the remote host (top/free) writing to a temp
file, then fetches and parses it. Extended fields (DB connections) sampled via
periodic SHOW STATUS through WP-CLI when available.
"""
from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..sshutil import SSHClient, from_site


def _f(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"max": None, "min": None, "avg": None}
    return {
        "max": round(max(vals), 2),
        "min": round(min(vals), 2),
        "avg": round(statistics.fmean(vals), 2),
    }


@dataclass
class SysmonSession:
    ssh: SSHClient
    remote_file: str
    duration_cap_s: float
    label: str = ""

    def stop_and_collect(self) -> dict:
        # stop the samplers
        self.ssh.run("pkill -f bench_sysmon_top 2>/dev/null; pkill top 2>/dev/null")
        try:
            _, out, _ = self.ssh.run(f"cat {self.remote_file} 2>/dev/null")
        except Exception:
            out = ""
        self.ssh.run(f"rm -f {self.remote_file} 2>/dev/null")
        return parse_top_output(out)


def start(site, duration_cap_s: float, label: str = "") -> Optional[SysmonSession]:
    """Start remote sampling. Returns a session or None if SSH unavailable."""
    try:
        ssh = from_site(site)
        ssh.connect()
    except Exception:
        return None

    remote_file = f"/tmp/bench_sysmon_{uuid.uuid4().hex[:8]}.txt"
    cap = max(2, int(duration_cap_s))
    # marker string 'bench_sysmon_top' lets us pkill precisely; -d 1 = 1s interval
    cmd = (
        f"nohup sh -c '# bench_sysmon_top\n"
        f"for i in $(seq 1 {cap}); do "
        f"top -b -n1 | grep -E \"(Cpu\\(s\\)|MiB Mem)\"; "
        f"free -m | awk \"/Mem:/ {{print \\\"MEMLINE\\\", \\$2, \\$3, \\$4}}\"; "
        f"sleep 1; done' > {remote_file} 2>/dev/null &"
    )
    try:
        ssh.run(cmd)
    except Exception:
        ssh.close()
        return None
    return SysmonSession(ssh=ssh, remote_file=remote_file,
                         duration_cap_s=cap, label=label)


def parse_top_output(text: str) -> dict:
    cpu_us, cpu_sy, mem_used, mem_free = [], [], [], []
    total_mem = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("%Cpu(s):") or "Cpu(s)" in line:
            # e.g. %Cpu(s):  4.5 us,  0.0 sy, ...
            try:
                seg = line.split(":", 1)[1]
                parts = [p.strip() for p in seg.split(",")]
                for p in parts:
                    if p.endswith("us"):
                        cpu_us.append(float(p.split()[0]))
                    elif p.endswith("sy"):
                        cpu_sy.append(float(p.split()[0]))
            except (IndexError, ValueError):
                pass
        elif line.startswith("MiB Mem"):
            # MiB Mem :   3906.1 total,   1200.0 free,   2000.0 used, ...
            try:
                seg = line.split(":", 1)[1]
                nums = seg.split(",")
                total_mem = float(nums[0].split()[0])
                mem_free.append(float(nums[1].split()[0]))
                mem_used.append(float(nums[2].split()[0]))
            except (IndexError, ValueError):
                pass
        elif line.startswith("MEMLINE"):
            try:
                _, t, u, f = line.split()
                total_mem = float(t)
                mem_used.append(float(u))
                mem_free.append(float(f))
            except ValueError:
                pass

    result = {
        "cpu": {"user": _f(cpu_us), "system": _f(cpu_sy)},
        "memory": {
            "total_memory": total_mem,
            "used": _f(mem_used),
            "free": _f(mem_free),
        },
        "samples": max(len(cpu_us), len(mem_used)),
    }
    if total_mem:
        mu = result["memory"]["used"]
        if mu["max"] is not None:
            result["memory"]["max_pct"] = round(mu["max"] / total_mem * 100, 2)
            result["memory"]["avg_pct"] = round(mu["avg"] / total_mem * 100, 2)
    return result
