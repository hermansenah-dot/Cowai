"""migrate_embeddings.py

One-time migration script to add embeddings to existing episodes.

Run this after updating to the vector-enabled memory system:
    python migrate_embeddings.py

Progress is saved - you can interrupt and resume safely.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

# Import our modules
import memory_vector as mv

DB_PATH = Path("memory") / "memory.db"
BATCH_SIZE = 50  # Embed this many at a time


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Nothing to migrate.")
        return

    # Check Ollama availability
    print(f"Checking Ollama connection (model: {mv.EMBED_MODEL})...")
    if not mv.is_ollama_available():
        print("ERROR: Ollama is not available or embedding model not found.")
        print(f"Make sure Ollama is running and has '{mv.EMBED_MODEL}' installed:")
        print(f"    ollama pull {mv.EMBED_MODEL}")
        sys.exit(1)

    print("Ollama OK!")
    dim = mv.get_embedding_dimension()
    print(f"Embedding dimension: {dim}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Count episodes needing embedding
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM episodes WHERE embedding IS NULL")
    total = cur.fetchone()["cnt"]

    if total == 0:
        print("All episodes already have embeddings. Nothing to do!")
        conn.close()
        return

    print(f"Found {total} episodes without embeddings.")
    print(f"Processing in batches of {BATCH_SIZE}...")
    print()

    processed = 0
    failed = 0
    start_time = time.time()

    while True:
        # Fetch batch of episodes without embeddings
        cur.execute(
            """
            SELECT id, text, tags
            FROM episodes
            WHERE embedding IS NULL
            LIMIT ?
            """,
            (BATCH_SIZE,),
        )
        rows = cur.fetchall()

        if not rows:
            break

        for row in rows:
            ep_id = row["id"]
            text = f"{row['text']} {row['tags']}".strip()

            embedding = mv.embed_text(text)
            if embedding:
                blob = mv.embedding_to_blob(embedding)
                cur.execute(
                    "UPDATE episodes SET embedding = ? WHERE id = ?",
                    (blob, ep_id),
                )
                processed += 1
            else:
                failed += 1
                print(f"  Failed to embed episode {ep_id}")

        conn.commit()

        # Progress update
        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        remaining = total - processed - failed
        eta = remaining / rate if rate > 0 else 0

        print(
            f"  Processed: {processed}/{total} | "
            f"Failed: {failed} | "
            f"Rate: {rate:.1f}/sec | "
            f"ETA: {eta:.0f}s"
        )

    conn.close()

    elapsed = time.time() - start_time
    print()
    print("=" * 50)
    print(f"Migration complete!")
    print(f"  Total processed: {processed}")
    print(f"  Failed: {failed}")
    print(f"  Time: {elapsed:.1f}s")


if __name__ == "__main__":
    migrate()
