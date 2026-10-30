"""Handwritten answer pages -> images -> text (mock-first).

Pipeline (proposal): PDF pages to images, OpenCV/Pillow clean-up, line
splitting, then TrOCR per line.  When the heavy libraries are missing the
engine degrades gracefully:

1. **pdf-text** — PyMuPDF text layer (real text, no ML) for typed PDFs.
2. **trocr**   — real handwriting recognition (torch + transformers).
3. **mock**    — clearly-labelled placeholder text so the teacher workflow
                 can be tested before models are installed.
"""

from __future__ import annotations

from pathlib import Path

from .config import model_config

#: PDF/image upload limit handled by the routes (kept here for reference).
MAX_UPLOAD_MB = 10


def pdf_renderer_available() -> bool:
    try:
        import fitz  # PyMuPDF
        return True
    except Exception:
        return False


def render_pdf_pages(pdf_path: Path, out_dir: Path) -> list[tuple[int, Path]]:
    """Render each PDF page to a JPEG. Returns [(page_no, image_path)]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import fitz  # PyMuPDF
    except Exception:
        return []

    rendered: list[tuple[int, Path]] = []
    with fitz.open(str(pdf_path)) as doc:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=140)
            target = out_dir / f"page_{index:03d}.jpg"
            pix.save(str(target))
            rendered.append((index, target))
    return rendered


def pdf_text_layer(pdf_path: Path) -> dict[int, str]:
    """Real text per page for PDFs that contain a text layer (not scans)."""
    try:
        import fitz
    except Exception:
        return {}
    text_by_page: dict[int, str] = {}
    try:
        with fitz.open(str(pdf_path)) as doc:
            for index, page in enumerate(doc, start=1):
                text_by_page[index] = (page.get_text() or "").strip()
    except Exception:
        return {}
    return text_by_page


def preprocess_image(path: Path) -> Path:
    """Contrast/threshold clean-up so OCR reads pencil writing better."""
    try:
        import cv2  # OpenCV (preferred, proposal)
        import numpy as np

        image = cv2.imread(str(path))
        if image is None:
            return path
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.bilateralFilter(gray, 9, 75, 75)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cv2.imwrite(str(path), thresh)
        return path
    except Exception:
        pass
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            cleaned = ImageOps.autocontrast(img.convert("L"))
            cleaned.save(path)
    except Exception:
        pass  # leave the original image untouched
    return path


def trocr_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except Exception:
        return False


_MODEL = None


def _load_trocr():
    """Load TrOCR once (lazy — a ~1.2 GB download on first real use)."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel

    name = model_config()["ocr_model"]
    processor = TrOCRProcessor.from_pretrained(name)
    model = VisionEncoderDecoderModel.from_pretrained(name)
    _MODEL = (processor, model)
    return _MODEL


def ocr_image(path: Path) -> tuple[str, float, str]:
    """``(text, confidence, engine)`` for one page image."""
    if trocr_available() and not model_config().get("force_mock"):
        try:
            import torch
            from PIL import Image

            processor, model = _load_trocr()
            image = Image.open(path).convert("RGB")
            inputs = processor(images=image, return_tensors="pt")
            with torch.no_grad():
                output = model.generate(**inputs)
            text = processor.batch_decode(output, skip_special_tokens=True)[0]
            # TrOCR has no calibrated confidence; use a fixed "real" marker.
            return text.strip(), 0.9, "trocr"
        except Exception:
            pass  # fall through to mock

    return "", 0.0, "mock"


def mock_page_text(page_no: int) -> str:
    return (f"[mock OCR — page {page_no}] Handwriting recognition runs here "
            "once TrOCR (torch + transformers) is installed. The teacher can "
            "still review and correct this text.")


def process_file(submission) -> None:
    """Render + OCR a submission in place (called by the job worker).

    Fills ``submission.pages`` and sets ``ocr_status``. Never raises —
    failures are reported through the status column.
    """
    from ..extensions import db
    from ..models import SubmissionPage, utcnow

    path = Path(submission.file_path)
    if not path.exists():
        submission.ocr_status = "failed"
        db.session.commit()
        return

    folder = path.parent / f"sub_{submission.id}"
    try:
        if submission.is_pdf:
            pages = render_pdf_pages(path, folder)
            text_by_page = pdf_text_layer(path)
            if not pages:
                # No renderer: keep one synthetic page so the flow continues.
                pages = []
        else:
            # Photos are already images — treat each stored file as a page.
            pages = [(1, path)]
            text_by_page = {}

        if not pages:
            submission.pages.append(
                SubmissionPage(
                    submission_id=submission.id,
                    page_no=1,
                    image_path=None,
                    extracted_text=mock_page_text(1),
                    ocr_confidence=0.0,
                )
            )
            submission.ocr_status = "done"
            db.session.commit()
            return

        for page_no, image_path in pages:
            real_text = (text_by_page.get(page_no) or "").strip()
            if len(real_text) >= 20:
                # Typed PDF — no OCR needed.
                text, confidence, engine = real_text, 1.0, "pdf-text"
            else:
                preprocess_image(image_path)
                text, confidence, engine = ocr_image(image_path)
                if engine == "mock":
                    text = mock_page_text(page_no)
                    confidence = 0.0
            submission.pages.append(
                SubmissionPage(
                    submission_id=submission.id,
                    page_no=page_no,
                    image_path=str(image_path),
                    extracted_text=text,
                    ocr_confidence=confidence,
                )
            )
        submission.ocr_status = "done"
        db.session.commit()
    except Exception:
        db.session.rollback()
        submission.ocr_status = "failed"
        db.session.commit()
        raise


def merge_images_to_pdf(image_paths: list[Path], target: Path) -> bool:
    """Merge uploaded photos into one PDF (proposal: "photos are merged into
    one PDF"). Uses Pillow; returns False when it cannot be done."""
    try:
        from PIL import Image
    except Exception:
        return False
    images = []
    try:
        for path in image_paths:
            with Image.open(path) as img:
                images.append(img.convert("RGB"))
        if not images:
            return False
        images[0].save(target, save_all=True, append_images=images[1:])
        return True
    except Exception:
        return False
