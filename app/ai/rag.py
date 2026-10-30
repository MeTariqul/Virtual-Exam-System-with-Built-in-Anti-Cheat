"""RAG knowledge base: chunk -> embed -> retrieve.

Teacher resource PDFs / notes are split into chunks, embedded, and stored.
The marking prompt pulls the top few chunks so marks stay tied to the
course material (proposal: "RAG knowledge base").

Storage: ChromaDB when it is installed (proposal's choice), otherwise a
built-in store — chunks keep their embedding as JSON in SQLite and we rank
with plain cosine similarity.  Embeddings: sentence-transformers when
available, otherwise a deterministic hashing embedder (mock mode) that still
ranks by shared keywords.
"""

from __future__ import annotations

import hashlib
import math
import re

from ..extensions import db
from ..models import Resource, ResourceChunk
from .config import model_config

#: Fallback embedding dimension for the hash embedder.
HASH_DIM = 256


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries."""
    cfg = model_config()
    size = size or int(cfg["chunk_size"])
    overlap = overlap if overlap is not None else int(cfg["chunk_overlap"])
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > size:  # huge paragraph: hard-slice it
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(para):
                chunks.append(para[start:start + size])
                start += size - overlap
            continue
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}" if current else para
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------

_ST_MODEL = None


def _sentence_model():
    global _ST_MODEL
    if _ST_MODEL is not None:
        return _ST_MODEL
    try:
        from sentence_transformers import SentenceTransformer

        _ST_MODEL = SentenceTransformer(model_config()["embedding_model"])
        return _ST_MODEL
    except Exception:
        _ST_MODEL = False
        return False


def embed(text: str) -> list[float]:
    """Embedding vector for a chunk (real model or deterministic mock)."""
    model = None if model_config().get("force_mock") else _sentence_model()
    if model:
        try:
            vector = model.encode(text)
            return [float(x) for x in vector]
        except Exception:
            pass
    return hash_embed(text)


def hash_embed(text: str) -> list[float]:
    """Mock embedder: hashed word counts, L2-normalised.

    Not semantic, but chunks sharing vocabulary with the query still rank
    first — enough to demo retrieval before sentence-transformers exists.
    """
    vec = [0.0] * HASH_DIM
    for word in re.findall(r"[a-z0-9']+", (text or "").lower()):
        digest = hashlib.md5(word.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "little") % HASH_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[index] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------
# Ingestion & retrieval
# --------------------------------------------------------------------------

def extract_resource_text(resource: Resource) -> str:
    """Text of an uploaded PDF (text layer) or of typed notes."""
    if not resource.file_path:
        return ""
    from pathlib import Path

    path = Path(resource.file_path)
    if not path.exists():
        return ""
    if path.suffix.lower() == ".pdf":
        from .ocr import pdf_text_layer

        pages = pdf_text_layer(path)
        return "\n\n".join(pages[key] for key in sorted(pages) if pages[key])
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def ingest_resource(resource_id: int) -> None:
    """Chunk + embed a resource (called by the job worker)."""
    resource = db.session.get(Resource, resource_id)
    if resource is None:
        return
    try:
        text = extract_resource_text(resource)
        chunks = chunk_text(text)
        # Rebuild from scratch so re-ingestion is idempotent.
        for old in list(resource.chunks):
            db.session.delete(old)
        db.session.flush()
        for index, piece in enumerate(chunks):
            db.session.add(ResourceChunk(
                resource_id=resource.id,
                chunk_index=index,
                page=None,
                text=piece,
                embedding=embed(piece),
            ))
        resource.status = "ready" if chunks else "failed"
        db.session.commit()
    except Exception:
        db.session.rollback()
        resource.status = "failed"
        db.session.commit()
        raise


def _vector_store():
    """ChromaDB collection when available, else None (builtin ranking)."""
    if model_config().get("force_mock"):
        return None
    try:
        import chromadb
        from pathlib import Path

        store = Path(__file__).resolve().parent.parent.parent / "instance" / "chroma"
        store.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(store))
        return client.get_or_create_collection("exam_resources")
    except Exception:
        return None


def mirror_to_chroma(resource_id: int) -> None:
    """Best-effort mirror into ChromaDB (keeps the proposal's store in sync)."""
    collection = _vector_store()
    if collection is None:
        return
    try:
        resource = db.session.get(Resource, resource_id)
        ids, documents, embeddings = [], [], []
        for chunk in resource.chunks:
            ids.append(f"r{resource.id}c{chunk.chunk_index}")
            documents.append(chunk.text)
            embeddings.append(chunk.embedding or embed(chunk.text))
        if ids:
            collection.upsert(ids=ids, documents=documents, embeddings=embeddings)
    except Exception:  # pragma: no cover - chroma is optional
        pass


def retrieve(question_text: str, exam_id: int, top_k: int | None = None) -> list[str]:
    """Top-K resource chunks for *question_text* inside one exam's materials."""
    top_k = top_k or int(model_config()["retrieval_top_k"])
    resources = Resource.query.filter_by(exam_id=exam_id, status="ready").all()
    candidates: list[ResourceChunk] = [
        chunk for resource in resources for chunk in resource.chunks
    ]
    if not candidates:
        return []

    query_vec = embed(question_text)
    scored = []
    for chunk in candidates:
        vector = chunk.embedding or embed(chunk.text)
        scored.append((cosine(query_vec, vector), chunk.text))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [text for score, text in scored[:top_k] if score > 0][:top_k]
