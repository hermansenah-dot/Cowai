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

# LRU cache for query embeddings (avoids re-embedding same queries)
_EMBED_CACHE: dict[str, List[float]] = {}
_CACHE_MAX_SIZE = 100  # Keep last 100 unique queries


def _cache_key(text: str, model: str) -> str:
    """Generate cache key for text+model."""
    return f"{model}:{text[:200].strip().lower()}"


# -------------------------
# Embedding Generation
# -------------------------

def embed_text(text: str, model: str = EMBED_MODEL, use_cache: bool = True) -> Optional[List[float]]:
    """
    Generate embedding vector for text using Ollama.
    
    Returns None if embedding fails (Ollama down, model not available, etc.)
    Uses LRU cache to avoid re-embedding identical queries.
    """
    global _EMBED_DIM, _EMBED_CACHE
    
    text = (text or "").strip()
    if not text:
        return None
    
    # Check cache first
    if use_cache:
        key = _cache_key(text, model)
        if key in _EMBED_CACHE:
            return _EMBED_CACHE[key]
    
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=10,  # Reduced from 30s
        )
        response.raise_for_status()
        data = response.json()
        embedding = data.get("embedding")
        
        if embedding and isinstance(embedding, list):
            # Auto-detect dimension
            if _EMBED_DIM is None:
                _EMBED_DIM = len(embedding)
            
            # Cache the result
            if use_cache:
                key = _cache_key(text, model)
                _EMBED_CACHE[key] = embedding
                # Evict oldest if cache too large
                if len(_EMBED_CACHE) > _CACHE_MAX_SIZE:
                    oldest_key = next(iter(_EMBED_CACHE))
                    del _EMBED_CACHE[oldest_key]
            
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
    Find most similar embeddings from candidates using vectorized numpy.
    
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
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []
    
    # Pre-normalize query once
    query_normalized = query_vec / query_norm
    
    # Batch process all candidates for vectorized similarity
    ids = []
    embeddings = []
    for item_id, blob in candidates:
        if blob:
            try:
                embeddings.append(blob_to_numpy(blob))
                ids.append(item_id)
            except Exception:
                continue
    
    if not embeddings:
        return []
    
    # Stack into matrix and compute all similarities at once
    matrix = np.vstack(embeddings)  # Shape: (n_candidates, dim)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1  # Avoid division by zero
    matrix_normalized = matrix / norms
    
    # Vectorized dot product
    similarities = matrix_normalized @ query_normalized  # Shape: (n_candidates,)
    
    # Filter and sort
    scored = [(ids[i], float(similarities[i])) for i in range(len(ids)) if similarities[i] >= threshold]
    scored.sort(key=lambda x: x[1], reverse=True)
    
    return scored[:top_k]


# -------------------------
# Utility
# -------------------------

def clear_embed_cache() -> int:
    """Clear the embedding cache. Returns number of entries cleared."""
    global _EMBED_CACHE
    count = len(_EMBED_CACHE)
    _EMBED_CACHE.clear()
    return count


def get_cache_stats() -> dict:
    """Get cache statistics."""
    return {
        "size": len(_EMBED_CACHE),
        "max_size": _CACHE_MAX_SIZE,
    }


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
