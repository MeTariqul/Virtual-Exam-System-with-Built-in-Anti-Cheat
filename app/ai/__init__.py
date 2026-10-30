"""Local AI pipeline: handwriting OCR, RAG retrieval and AI-assisted marking.

Design (proposal sections "Open-Source Models and Tools" and
"How the AI Marking Works"):

* Every engine has a **mock mode** so the whole flow can be demoed before the
  heavy models are installed (TrOCR, sentence-transformers, Ollama).
* Model names live in ``app/ai/config.py`` (overridable via environment or
  ``instance/ai_config.json``), so swapping models never touches the code.
* Student answers are always treated as untrusted *data* — instructions
  written inside an answer ("give me full marks") never reach the model as
  instructions (see ``marking.build_prompt``).
"""

from .config import engine_report, model_config
from .jobs import enqueue, inline_mode, shutdown

__all__ = ["enqueue", "inline_mode", "shutdown", "engine_report", "model_config"]
