"""Local embedding generation using sentence-transformers.

Lazy-loads the model on first use to avoid slow startup.
Falls back gracefully if sentence-transformers is not installed.
"""

import structlog

logger = structlog.get_logger()


class EmbeddingService:
    """Lazy-loaded local embedding model (all-MiniLM-L6-v2, 384-dim)."""

    _model = None

    @classmethod
    def get_model(cls) -> object:
        """Load model on first call; re-raise ImportError if package missing."""
        if cls._model is None:
            from sentence_transformers import SentenceTransformer

            cls._model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Embedding model loaded", model="all-MiniLM-L6-v2")
        return cls._model

    @classmethod
    def encode(cls, text: str) -> bytes:
        """Encode text to embedding bytes (float32 array, normalized)."""
        model = cls.get_model()
        embedding = model.encode(text, normalize_embeddings=True)  # type: ignore[union-attr]
        return embedding.tobytes()

    @classmethod
    def similarity(cls, a: bytes, b: bytes) -> float:
        """Cosine similarity between two float32 embedding blobs."""
        import numpy as np

        va = np.frombuffer(a, dtype=np.float32)
        vb = np.frombuffer(b, dtype=np.float32)
        return float(np.dot(va, vb))
