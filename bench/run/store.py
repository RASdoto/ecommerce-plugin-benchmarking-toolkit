"""
Append-only results store with resume support (Phase 8).

Each matrix cell writes one JSON line keyed by (platform, entity, volume,
concurrency, operation). The runner consults `completed_keys()` to skip cells
already done, so a killed run resumes cleanly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


class ResultStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    @staticmethod
    def cell_key(row: dict) -> str:
        return "|".join(str(row.get(k, "")) for k in
                        ("platform", "operation", "entity", "volume", "concurrency"))

    def completed_keys(self) -> set[str]:
        keys = set()
        for row in self.read_all():
            keys.add(self.cell_key(row))
        return keys

    def append(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def read_all(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
