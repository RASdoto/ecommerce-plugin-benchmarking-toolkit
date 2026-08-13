"""
Database inspector ([SSH], Phase 6/7). Runs SQL over SSH via WP-CLI
(`wp db query`) to capture table counts/sizes, row counts, autoload weight,
index footprint, and EXPLAIN verdicts on captured hot queries.
"""
from __future__ import annotations

import re
from typing import Optional

from ..sshutil import SSHClient


class DBProbe:
    def __init__(self, ssh: SSHClient, wp_path: str, wp_cli: str = "wp"):
        self.ssh = ssh
        self.wp_path = wp_path
        self.wp = wp_cli

    def _query(self, sql: str) -> str:
        # --skip-column-names gives tab-separated raw rows
        safe = sql.replace('"', '\\"')
        cmd = f'cd {self.wp_path} && {self.wp} db query "{safe}" --skip-column-names 2>/dev/null'
        code, out, err = self.ssh.run(cmd)
        return out if code == 0 else ""

    def table_stats(self, prefix: str = "") -> dict:
        sql = (
            "SELECT table_name, table_rows, "
            "ROUND((data_length)/1024/1024,2) AS data_mb, "
            "ROUND((index_length)/1024/1024,2) AS index_mb "
            "FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        )
        out = self._query(sql)
        tables = {}
        total_data = total_index = 0.0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, rows, data_mb, index_mb = parts[0], parts[1], parts[2], parts[3]
            if prefix and not name.startswith(prefix):
                pass  # still count in totals but tag prefix separately
            try:
                d = float(data_mb or 0)
                i = float(index_mb or 0)
            except ValueError:
                d = i = 0.0
            tables[name] = {"rows": _int(rows), "data_mb": d, "index_mb": i}
            total_data += d
            total_index += i
        return {
            "table_count": len(tables),
            "total_data_mb": round(total_data, 2),
            "total_index_mb": round(total_index, 2),
            "tables": tables,
        }

    def row_count(self, table: str) -> Optional[int]:
        out = self._query(f"SELECT COUNT(*) FROM {table}")
        first = out.strip().splitlines()[0] if out.strip() else ""
        return _int(first)

    def autoload_weight(self, prefix: str = "wp_") -> dict:
        sql = (
            f"SELECT COUNT(*), ROUND(SUM(LENGTH(option_value))/1024,2) "
            f"FROM {prefix}options WHERE autoload='yes'"
        )
        out = self._query(sql).strip()
        if not out:
            return {"count": None, "kb": None}
        parts = out.splitlines()[0].split("\t")
        return {"count": _int(parts[0]) if parts else None,
                "kb": _float(parts[1]) if len(parts) > 1 else None}

    def explain(self, query: str) -> dict:
        out = self._query("EXPLAIN " + query)
        verdict = {"raw": out.strip(), "full_scan": False, "uses_index": None}
        for line in out.splitlines():
            cols = line.split("\t")
            # EXPLAIN columns: id select_type table partitions type possible_keys key ...
            if "ALL" in cols:
                verdict["full_scan"] = True
            # key column (index used) — heuristic position 6
            if len(cols) > 6:
                verdict["uses_index"] = cols[6] not in ("", "NULL")
        return verdict


def _int(s: str):
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return None


def _float(s: str):
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None
