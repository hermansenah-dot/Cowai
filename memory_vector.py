"""memory_vector.py

Vector embedding utilities for semantic memory retrieval.

Uses Ollama's /api/embeddings endpoint to generate embeddings locally.
Embeddings are stored as BLOBs in SQLite alongside text memories.

Requires: numpy (for cosine similarity), requests (already used by ai.py)
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

import numpy as np
import requests


# -------------------------
# Configuration
# -------------------------

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"  # Good balance of quality/speed, 768 dimensions

# Dimension of embeddings (nomic-embed-text = 768)
# Will be auto-detected on first call
_EMBED_DIM: Optional[int] = None


# -------------------------
# Embedding Generation
# -------------------------

def embed_text(text: str, model: str = EMBED_MODEL) -> Optional[List[float]]:
    """
    Generate embedding vector for text using Ollama.
    
    Returns None if embedding fails (Ollama down, model not available, etc.)
    """
    global _EMBED_DIM
    
    text = (text or "").strip()
    if not text:
        return None
    
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        embedding = data.get("embedding")
        
        if embedding and isinstance(embedding, list):
            # Auto-detect dimension
            if _EMBED_DIM is None:
                _EMBED_DIM = len(embedding)
            return embedding
        return None
        
    except Exception as e:
        print(f"[memory_vector] Embedding failed: {e}")
        return None


def embed_texts_batch(texts: List[str], model: str = EMBED_MODEL) -> List[Optional[List[float]]]:
    """
    Embed multiple texts. Returns list of embeddings (None for failures).
    
    Note: Ollama doesn't have a true batch endpoint, so this is sequential.
    Could be parallelized with threads if needed.
    """
    return [embed_text(t, model) for t in texts]


# -------------------------
# Serialization (for SQLite BLOB storage)
# -------------------------

def embedding_to_blob(embedding: List[float]) -> bytes:
    """Pack float list into bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def blob_to_embedding(blob: bytes) -> List[float]:
    """Unpack bytes back to float list."""
    count = len(blob) // 4  # 4 bytes per float
    return list(struct.unpack(f"{count}f", blob))


def embedding_to_numpy(embedding: List[float]) -> np.ndarray:
    """Convert embedding list to numpy array for math operations."""
    return np.array(embedding, dtype=np.float32)


def blob_to_numpy(blob: bytes) -> np.ndarray:
    """Convert BLOB directly to numpy array."""
    return np.frombuffer(blob, dtype=np.float32)


# -------------------------
# Similarity Functions
# -------------------------

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Compute cosine similarity between two vectors.
    Returns value in [-1, 1], higher = more similar.
    """
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    
    if norm_a == 0 or norm_b == 0:
        return 0.0
    
    return float(np.dot(a, b) / (norm_a * norm_b))


def find_similar(
    query_embedding: List[float],
    candidates: List[Tuple[int, bytes]],  # List of (id, embedding_blob)
    top_k: int = 5,
    threshold: float = 0.3,
) -> List[Tuple[int, float]]:
    """
    Find most similar embeddings from candidates.
    
    Args:
        query_embedding: The query vector
        candidates: List of (id, embedding_blob) tuples
        top_k: Maximum number of results
        threshold: Minimum similarity score (0-1)
    
    Returns:
        List of (id, similarity_score) tuples, sorted by score descending
    """
    if not query_embedding or not candidates:
        return []
    
    query_vec = embedding_to_numpy(query_embedding)
    
    scored: List[Tuple[int, float]] = []
    for item_id, blob in candidates:
        if not blob:
            continue
        
        try:
            candidate_vec = blob_to_numpy(blob)
            sim = cosine_similarity(query_vec, candidate_vec)
            
            if sim >= threshold:
                scored.append((item_id, sim))
        except Exception:
            continue
    
    # Sort by similarity descending
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


# -------------------------
# Utility
# -------------------------

def is_ollama_available(model: str = EMBED_MODEL) -> bool:
    """Check if Ollama is running and has the embedding model."""
    try:
        # Quick test embedding
        result = embed_text("test", model)
        return result is not None
    except Exception:
        return False


def get_embedding_dimension() -> Optional[int]:
    """Get the dimension of embeddings (after first embed call)."""
    global _EMBED_DIM
    if _EMBED_DIM is None:
        # Force detection
        embed_text("dimension check")
    return _EMBED_DIM
