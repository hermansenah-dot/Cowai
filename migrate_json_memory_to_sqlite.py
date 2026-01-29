"""migrate_json_memory_to_sqlite.py

One-time migration tool:
- Reads legacy per-user JSON files in memory/users/*.json
- Copies known keys into SQLite facts table

Run:
  python migrate_json_memory_to_sqlite.py
"""

from __future__ import annotations

import json
from pathlib import Path

from memory_sqlite import SQLiteMemory


LEGACY_DIR = Path("memory") / "users"
DB_PATH = "memory.db"


def main() -> None:
    mem = SQLiteMemory(DB_PATH)

    if not LEGACY_DIR.exists():
        print("No legacy directory found:", LEGACY_DIR)
        return

    files = 0
    facts = 0

    for fp in LEGACY_DIR.glob("*.json"):
        files += 1
        user_id = fp.stem

        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print("Skip (bad json):", fp, e)
            continue

        def put(key: str, value, conf: float = 0.8):
            nonlocal facts
            if value is None:
                return
            val = str(value).strip()
            if not val:
                return
            mem.upsert_fact(int(user_id), key, val, confidence=conf)
            facts += 1

        put("name", data.get("name"), 0.9)
        put("preferred_language", data.get("preferred_language"), 0.9)

        likes = data.get("likes")
        if isinstance(likes, list) and likes:
            put("likes", ", ".join(map(str, likes)), 0.7)

        dislikes = data.get("dislikes")
        if isinstance(dislikes, list) and dislikes:
            put("dislikes", ", ".join(map(str, dislikes)), 0.7)

        # Optional: store voice_enabled in SQLite too (commands still read JSON today)
        if "voice_enabled" in data:
            put("voice_enabled", bool(data.get("voice_enabled")), 0.8)

    print(f"Done. Files scanned: {files}, facts written: {facts}")


if __name__ == "__main__":
    main()
