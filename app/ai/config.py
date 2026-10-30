"""AI model configuration.

Model names come from (in order of precedence): environment variables,
``instance/ai_config.json`` (created with the defaults on first run), then
the defaults below — so a department can change models without editing code
(proposal: "The model names are kept in a configuration file so they can be
changed easily").
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    # Handwriting to text (transformers pipeline; needs torch).
    "ocr_model": "microsoft/trocr-base-handwritten",
    # Marking LLM served by Ollama (proposal: qwen2.5 7B, 3B on weak machines).
    "marking_model": "qwen2.5:7b-instruct",
    "marking_model_small": "qwen2.5:3b-instruct",
    # Embeddings for the RAG knowledge base.
    "embedding_model": "BAAI/bge-small-en-v1.5",
    # Ollama endpoint.
    "ollama_host": "http://127.0.0.1:11434",
    # Chunk size for resource documents.
    "chunk_size": 800,
    "chunk_overlap": 150,
    # Top-K retrieved chunks added to the marking prompt.
    "retrieval_top_k": 4,
}

ENV_KEYS = {
    "ocr_model": "EXAM_OCR_MODEL",
    "marking_model": "EXAM_MARKING_MODEL",
    "marking_model_small": "EXAM_MARKING_MODEL_SMALL",
    "embedding_model": "EXAM_EMBED_MODEL",
    "ollama_host": "OLLAMA_HOST",
}

_CACHE: dict | None = None


def _config_file() -> Path:
    # instance/ai_config.json sits next to the database.
    return Path(__file__).resolve().parent.parent.parent / "instance" / "ai_config.json"


def ensure_config_file() -> Path:
    """Create ``instance/ai_config.json`` with the defaults on first run.

    The file only needs the keys you want to change; anything missing falls
    back to the defaults above. Safe to call repeatedly.
    """
    path = _config_file()
    if path.is_file():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        reload_config()
    except OSError:  # pragma: no cover - read-only install
        pass
    return path


def model_config() -> dict:
    """Merged configuration: environment > file > defaults."""
    global _CACHE
    if _CACHE is not None:
        return dict(_CACHE)

    merged = dict(DEFAULTS)
    # 1) instance/ai_config.json (created with the defaults on first run)
    try:
        file_values = json.loads(_config_file().read_text(encoding="utf-8"))
        if isinstance(file_values, dict):
            for key, value in file_values.items():
                if key in merged and isinstance(value, type(merged[key])):
                    merged[key] = value
    except (OSError, ValueError):
        pass  # no config file yet — defaults are fine
    # 2) environment wins, so a deployment can override without editing files
    for key, env in ENV_KEYS.items():
        value = os.environ.get(env)
        if value:
            merged[key] = value

    # Force mock mode regardless of installed libraries.
    if os.environ.get("EXAM_AI_MOCK") == "1":
        merged["force_mock"] = True
    _CACHE = merged
    return dict(merged)


def reload_config() -> None:
    """Drop the cache so the next ``model_config()`` re-reads the file
    (call it after editing ``instance/ai_config.json``)."""
    global _CACHE
    _CACHE = None


# -- Ollama reachability (cached: probing every answer would stall marking) --
_PROBE: dict = {"at": 0.0, "up": False}
_PROBE_TTL = 30.0  # seconds


def ollama_up() -> bool:
    """Is the Ollama server reachable? Result cached for 30 seconds.

    Marking may ask this once per answer — without the cache an absent
    server would cost its full connect timeout every single time.
    """
    import time
    import urllib.request

    cfg = model_config()
    if cfg.get("force_mock"):
        return False
    now = time.monotonic()
    if now - _PROBE["at"] < _PROBE_TTL:
        return _PROBE["up"]
    try:
        with urllib.request.urlopen(cfg["ollama_host"] + "/api/tags", timeout=2) as r:
            up = r.status == 200
    except Exception:
        up = False
    _PROBE.update(at=now, up=up)
    return up


def engine_report() -> dict:
    """Which engines are real or mocked right now — shown on the AI status page."""
    cfg = model_config()

    def _has(module: str) -> bool:
        try:
            __import__(module)
            return True
        except Exception:
            return False

    ollama_up_flag = ollama_up()

    return {
        "force_mock": bool(cfg.get("force_mock")),
        "ocr": {
            "engine": "mock" if cfg.get("force_mock") else (
                "trocr" if _has("transformers") and _has("torch") else "pdf-text/mock"
            ),
            "model": cfg["ocr_model"],
            "pdf_renderer": "pymupdf" if _has("fitz") else "unavailable",
        },
        "embeddings": {
            "engine": "mock" if cfg.get("force_mock") else (
                cfg["embedding_model"] if _has("sentence_transformers") else "hash"
            ),
            "vector_store": "chromadb" if _has("chromadb") else "builtin",
        },
        "marking": {
            "engine": "mock" if cfg.get("force_mock") or not ollama_up_flag else "ollama",
            "model": cfg["marking_model"],
            "ollama_host": cfg["ollama_host"],
            "ollama_up": ollama_up_flag,
        },
    }
