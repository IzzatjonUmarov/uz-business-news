"""Хранилище уже обработанных ссылок (JSON, чтобы его можно было коммитить в git из GitHub Actions)."""

from __future__ import annotations

import json
import time
from pathlib import Path

KEEP_SECONDS = 90 * 24 * 3600


class State:
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, float] = {}
        self.sources: dict[str, dict] = {}
        self.meta: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.seen = data.get("seen", {})
            self.sources = data.get("sources", {})
            self.meta = data.get("meta", {})

    def is_seen(self, url: str) -> bool:
        return url in self.seen

    def mark(self, url: str) -> None:
        self.seen[url] = time.time()

    def source_initialized(self, source_id: str) -> bool:
        return self.sources.get(source_id, {}).get("initialized", False)

    def record_source(self, source_id: str, ok: bool, found: int, error: str | None = None) -> None:
        info = self.sources.setdefault(source_id, {})
        info["last_check"] = time.time()
        info["last_found"] = found
        info["fail_streak"] = 0 if ok else info.get("fail_streak", 0) + 1
        info["last_error"] = error
        if ok:
            info["initialized"] = True

    def save(self) -> None:
        cutoff = time.time() - KEEP_SECONDS
        self.seen = {u: t for u, t in self.seen.items() if t >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"seen": self.seen, "sources": self.sources, "meta": self.meta},
                       ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)
