"""Local-first document rendering, OCR, PII suggestions and flattened export.

No document bytes or OCR text are sent over the network. Presidio and its spaCy
model are optional at import time; the installer provisions them for a richer
local PII scan. A deterministic OCR/regex fallback remains available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import re
from typing import Iterable, Any

import fitz
import numpy as np
import pytesseract
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps, ImageFilter

try:
    import cv2
except Exception:
    cv2 = None

_ANALYZER = None
_ANALYZER_TRIED = False


@dataclass
class Region:
    x: float
    y: float
    w: float
    h: float
    label: str = "Sensitive information"
    score: float = 0.0


@dataclass
class Redaction:
    x: float
    y: float
    w: float
    h: float
    mode: str = "black"  # black or pixelate


@dataclass
class Page:
    image: Image.Image
    page_size_pt: tuple[float, float] | None = None
    detections: list[Region] = field(default_factory=list)
    redactions: list[Redaction] = field(default_factory=list)
    ocr_text: str = ""
    native_text_lines: list[list[dict[str, Any]]] = field(default_factory=list)


class Document:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.pages: list[Page] = []
        self.kind = "image"
        self._load()

    def _load(self) -> None:
        suffix = self.path.suffix.lower()
        if suffix == ".pdf":
            self.kind = "pdf"
            with fitz.open(self.path) as pdf:
                for page in pdf:
                    # 2.5x corresponds to ~180 dpi for a 72 dpi PDF page.
                    render_scale = 2.5
                    pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    native_grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
                    for word in page.get_text("words"):
                        x0, y0, x1, y1, word_text = word[:5]
                        block_no, line_no = int(word[5]), int(word[6])
                        if not str(word_text).strip():
                            continue
                        native_grouped.setdefault((block_no, line_no), []).append({
                            "text": str(word_text),
                            "x": max(0, int(round(float(x0) * render_scale))),
                            "y": max(0, int(round(float(y0) * render_scale))),
                            "w": max(1, int(round((float(x1) - float(x0)) * render_scale))),
                            "h": max(1, int(round((float(y1) - float(y0)) * render_scale))),
                        })
                    native_lines = list(native_grouped.values())
                    for line in native_lines:
                        line.sort(key=lambda item: item["x"])
                    native_lines.sort(key=lambda line: (min(w["y"] for w in line), min(w["x"] for w in line)))
                    self.pages.append(Page(
                        img, (float(page.rect.width), float(page.rect.height)),
                        native_text_lines=native_lines,
                    ))
        else:
            with Image.open(self.path) as source:
                dpi = source.info.get("dpi", (150, 150))
                try:
                    dpi_x = float(dpi[0]) if dpi and dpi[0] else 150.0
                    dpi_y = float(dpi[1]) if dpi and dpi[1] else 150.0
                except (TypeError, ValueError, IndexError):
                    dpi_x = dpi_y = 150.0
                image = ImageOps.exif_transpose(source).convert("RGB")
                page_size = (image.width * 72.0 / dpi_x, image.height * 72.0 / dpi_y)
                self.pages.append(Page(image.copy(), page_size))
        if not self.pages:
            raise ValueError("The selected document contains no pages.")

    def detect(self, page_index: int) -> list[Region]:
        """OCR a page, then locate PII spans and map them back to image boxes."""
        page = self.pages[page_index]
        image = page.image
        ocr_lines = _ocr_lines(image)
        ocr_text, ocr_spans = _join_lines(ocr_lines)
        native_text, native_spans = _join_lines(page.native_text_lines) if page.native_text_lines else ("", [])
        # Prefer the PDF text layer for legible digital documents, but also scan
        # OCR output to catch images embedded inside those PDFs.
        layers = []
        if native_text.strip():
            layers.append((native_text, native_spans))
        if ocr_text.strip():
            layers.append((ocr_text, ocr_spans))
        page.ocr_text = "\n".join(text for text, _ in layers)
        found: list[Region] = []

        # Microsoft Presidio combines recognizers (patterns, context, checksum)
        # and local NER. Indian PAN/Aadhaar recognizers are included upstream.
        analyzer = _get_presidio_analyzer()
        fallback_patterns = [
            ("Aadhaar number", re.compile(r"(?<!\d)(?:\d[\s-]*){11}\d(?!\d)")),
            ("PAN", re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])", re.I)),
            ("Email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?!\w)")),
            ("Indian mobile", re.compile(r"(?<!\d)(?:(?:\+?91)[\s-]?)?[6-9]\d{9}(?!\d)")),
            ("Payment card", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
            # Contextual patterns reduce false positives for account numbers:
            # only match digit strings when an account label is nearby.
            ("Bank account number", re.compile(r"\b(?:account|acct|a/c)\s*(?:number|no\.?|#)?[^\n\d]{0,18}([0-9][0-9 -]{7,18}[0-9])", re.I)),
            ("IFSC code", re.compile(r"(?<![A-Z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Z0-9])", re.I)),
        ]
        for layer_text, word_spans in layers:
            if analyzer is not None and layer_text.strip():
                try:
                    results = analyzer.analyze(
                        text=layer_text,
                        language="en",
                        entities=[
                            "PERSON", "LOCATION", "ORGANIZATION", "EMAIL_ADDRESS",
                            "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE", "IP_ADDRESS",
                            "URL", "DATE_TIME", "IN_AADHAAR", "IN_PAN", "IN_PASSPORT",
                            "IN_VOTER", "IN_VEHICLE_REGISTRATION", "IN_GSTIN",
                        ],
                        score_threshold=0.25,
                    )
                    for result in results:
                        box = _span_to_box(result.start, result.end, word_spans)
                        if box:
                            x, y, w, h = box
                            found.append(Region(x, y, w, h, _pretty_entity(result.entity_type), float(result.score)))
                except Exception:
                    # The deterministic fallback remains available if a
                    # recognizer/model cannot initialize for any reason.
                    pass
            for label, pattern in fallback_patterns:
                for match in pattern.finditer(layer_text):
                    if label == "Payment card" and not _passes_luhn(match.group(0)):
                        continue
                    span_start, span_end = match.span(1) if label == "Bank account number" and match.lastindex else match.span()
                    box = _span_to_box(span_start, span_end, word_spans)
                    if box:
                        x, y, w, h = box
                        found.append(Region(x, y, w, h, label, 0.55))

        # QR/barcode-like regions are useful to review because they may encode
        # UPI, identity, contact or ticket data. QR support is optional.
        if cv2 is not None:
            try:
                arr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
                detector = cv2.QRCodeDetector()
                ok_multi, decoded, points, _ = detector.detectAndDecodeMulti(arr)
                polygons = points if ok_multi and points is not None else None
                if polygons is None:
                    ok, one_points = detector.detect(arr)
                    polygons = [one_points[0]] if ok and one_points is not None else []
                if polygons is None:
                    polygons = []
                for polygon in polygons:
                    pts = np.asarray(polygon).reshape(-1, 2)
                    x0, y0 = np.floor(pts.min(axis=0)).astype(int)
                    x1, y1 = np.ceil(pts.max(axis=0)).astype(int)
                    pad = max(5, int(min(x1 - x0, y1 - y0) * 0.05))
                    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
                    x1, y1 = min(image.width, x1 + pad), min(image.height, y1 + pad)
                    found.append(Region(x0, y0, x1 - x0, y1 - y0, "QR code", 0.9))
            except Exception:
                pass

        page.detections = _deduplicate_regions(found)
        return page.detections


def _get_presidio_analyzer():
    global _ANALYZER, _ANALYZER_TRIED
    if _ANALYZER_TRIED:
        return _ANALYZER
    _ANALYZER_TRIED = True
    try:
        from presidio_analyzer import AnalyzerEngine
        # Presidio's default local NLP model is spaCy en_core_web_lg.
        # It and all recognizers run in-process; no HTTP service is used.
        _ANALYZER = AnalyzerEngine()
    except Exception:
        _ANALYZER = None
    return _ANALYZER


def detection_stack_status() -> str:
    """Return an honest, lightweight indication of which detector is installed."""
    try:
        from importlib.util import find_spec
        if find_spec("presidio_analyzer") and find_spec("en_core_web_lg"):
            return "Presidio + spaCy en_core_web_lg ready"
    except (ImportError, ValueError):
        pass
    return "OCR pattern fallback · PII model not installed"


def _pretty_entity(entity: str) -> str:
    labels = {
        "IN_AADHAAR": "Aadhaar number", "IN_PAN": "PAN",
        "IN_PASSPORT": "Passport number", "IN_VOTER": "Voter ID",
        "IN_VEHICLE_REGISTRATION": "Vehicle registration", "IN_GSTIN": "GSTIN",
        "PERSON": "Person name", "LOCATION": "Location / address",
        "ORGANIZATION": "Organization", "EMAIL_ADDRESS": "Email",
        "PHONE_NUMBER": "Phone number", "CREDIT_CARD": "Payment card",
        "IBAN_CODE": "Bank account / IBAN", "IP_ADDRESS": "IP address",
        "URL": "URL", "DATE_TIME": "Date / time",
    }
    return labels.get(entity, entity.replace("_", " ").title())


def _prepare_ocr_image(image: Image.Image) -> tuple[Image.Image, float]:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    # Small scans are upscaled to improve character recognition. Coordinates
    # are mapped back to source pixels after OCR.
    scale = min(2.0, max(1.0, 1700.0 / max(1, gray.width)))
    if scale > 1.01:
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.Resampling.LANCZOS)
    gray = ImageEnhance.Contrast(gray).enhance(1.15)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray, scale


def _ocr_lines(image: Image.Image) -> list[list[dict[str, Any]]]:
    prepared, scale = _prepare_ocr_image(image)
    candidates = []
    for psm in (6, 11):
        data = pytesseract.image_to_data(
            prepared,
            lang="eng",
            output_type=pytesseract.Output.DICT,
            config=f"--oem 3 --psm {psm} -c preserve_interword_spaces=1",
        )
        words = []
        confidences = []
        for i, raw in enumerate(data.get("text", [])):
            text = (raw or "").strip()
            try:
                confidence = float(data["conf"][i])
            except (TypeError, ValueError, KeyError):
                confidence = -1.0
            if not text or confidence < 15:
                continue
            x = int(round(int(data["left"][i]) / scale))
            y = int(round(int(data["top"][i]) / scale))
            w = max(1, int(round(int(data["width"][i]) / scale)))
            h = max(1, int(round(int(data["height"][i]) / scale)))
            words.append({
                "text": text, "x": max(0, x), "y": max(0, y),
                "w": w, "h": h, "conf": confidence,
                "key": (data["page_num"][i], data["block_num"][i], data["par_num"][i], data["line_num"][i]),
            })
            confidences.append(confidence)
        avg_conf = sum(confidences) / max(1, len(confidences))
        score = len(words) * 1.0 + avg_conf * 0.04
        candidates.append((score, words))

    # If both standard layouts produce almost nothing, try sparse text on a
    # thresholded image; this helps screenshots and faint scanned documents.
    if max((len(words) for _, words in candidates), default=0) < 5 and cv2 is not None:
        arr = np.array(prepared)
        _, thresholded = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        threshold_image = Image.fromarray(thresholded)
        data = pytesseract.image_to_data(
            threshold_image, lang="eng", output_type=pytesseract.Output.DICT,
            config="--oem 3 --psm 11",
        )
        words = []
        confidences = []
        for i, raw in enumerate(data.get("text", [])):
            text = (raw or "").strip()
            try:
                confidence = float(data["conf"][i])
            except (TypeError, ValueError, KeyError):
                confidence = -1
            if not text or confidence < 15:
                continue
            words.append({
                "text": text,
                "x": max(0, int(round(int(data["left"][i]) / scale))),
                "y": max(0, int(round(int(data["top"][i]) / scale))),
                "w": max(1, int(round(int(data["width"][i]) / scale))),
                "h": max(1, int(round(int(data["height"][i]) / scale))),
                "conf": confidence,
                "key": (data["page_num"][i], data["block_num"][i], data["par_num"][i], data["line_num"][i]),
            })
            confidences.append(confidence)
        candidates.append((len(words) + (sum(confidences) / max(1, len(confidences))) * 0.04, words))

    best_words = max(candidates, key=lambda item: item[0])[1] if candidates else []
    grouped: dict[tuple, list[dict[str, Any]]] = {}
    for word in best_words:
        grouped.setdefault(word["key"], []).append(word)
    lines = []
    for words in grouped.values():
        words.sort(key=lambda word: word["x"])
        lines.append(words)
    lines.sort(key=lambda words: (min(w["y"] for w in words), min(w["x"] for w in words)))
    return lines


def _join_lines(lines: list[list[dict[str, Any]]]):
    chunks: list[str] = []
    spans: list[dict[str, Any]] = []
    cursor = 0
    for line in lines:
        if chunks:
            chunks.append("\n")
            cursor += 1
        for index, word in enumerate(line):
            if index:
                chunks.append(" ")
                cursor += 1
            start = cursor
            chunks.append(word["text"])
            cursor += len(word["text"])
            spans.append({"start": start, "end": cursor, **word})
    return "".join(chunks), spans


def _span_to_box(start: int, end: int, word_spans: list[dict[str, Any]]):
    touched = [w for w in word_spans if w["start"] < end and w["end"] > start]
    if not touched:
        return None
    x0 = min(w["x"] for w in touched)
    y0 = min(w["y"] for w in touched)
    x1 = max(w["x"] + w["w"] for w in touched)
    y1 = max(w["y"] + w["h"] for w in touched)
    pad_x = max(4, int((x1 - x0) * 0.07))
    pad_y = max(3, int((y1 - y0) * 0.14))
    return (max(0, x0 - pad_x), max(0, y0 - pad_y), x1 - max(0, x0 - pad_x) + pad_x, y1 - max(0, y0 - pad_y) + pad_y)


def _passes_luhn(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _deduplicate_regions(regions: list[Region]) -> list[Region]:
    result: list[Region] = []
    for candidate in sorted(regions, key=lambda r: (r.score, r.w * r.h), reverse=True):
        if candidate.w <= 0 or candidate.h <= 0:
            continue
        duplicate_index = None
        for i, current in enumerate(result):
            ix = max(0, min(candidate.x + candidate.w, current.x + current.w) - max(candidate.x, current.x))
            iy = max(0, min(candidate.y + candidate.h, current.y + current.h) - max(candidate.y, current.y))
            inter = ix * iy
            union = candidate.w * candidate.h + current.w * current.h - inter
            if union and inter / union > 0.50:
                duplicate_index = i
                break
        if duplicate_index is None:
            result.append(candidate)
        elif candidate.score > result[duplicate_index].score:
            result[duplicate_index] = candidate
    result.sort(key=lambda r: (r.y, r.x))
    return result


def _clamp_box(box, width: int, height: int) -> tuple[int, int, int, int]:
    x0 = max(0, min(width, int(round(box.x))))
    y0 = max(0, min(height, int(round(box.y))))
    x1 = max(0, min(width, int(round(box.x + box.w))))
    y1 = max(0, min(height, int(round(box.y + box.h))))
    return x0, y0, x1, y1


def apply_redactions(image: Image.Image, redactions: Iterable[Redaction]) -> Image.Image:
    result = image.convert("RGB").copy()
    for redaction in redactions:
        x0, y0, x1, y1 = _clamp_box(redaction, result.width, result.height)
        if x1 <= x0 or y1 <= y0:
            continue
        if redaction.mode == "pixelate":
            region = result.crop((x0, y0, x1, y1))
            tiny_w, tiny_h = max(1, (x1 - x0) // 12), max(1, (y1 - y0) // 12)
            pixelated = region.resize((tiny_w, tiny_h), Image.Resampling.BILINEAR)
            pixelated = pixelated.resize((x1 - x0, y1 - y0), Image.Resampling.NEAREST)
            result.paste(pixelated, (x0, y0))
        else:
            ImageDraw.Draw(result).rectangle((x0, y0, x1 - 1, y1 - 1), fill=(0, 0, 0))
    return result


def add_watermark(image: Image.Image, purpose: str, date_text: str) -> Image.Image:
    purpose = purpose.strip()
    if not purpose:
        return image
    result = image.convert("RGB").copy()
    label = f"{purpose.upper()}  •  {date_text.strip()}" if date_text.strip() else purpose.upper()
    font_size = max(18, min(56, result.width // 32))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()
    overlay = Image.new("RGBA", (result.width * 2, result.height * 2), (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), label, font=font)
    text_width = bbox[2] - bbox[0]
    step_x = max(text_width + 100, overlay.width // 2)
    step_y = max(font_size * 4, 90)
    for y in range(-overlay.height, overlay.height * 2, step_y):
        for x in range(-overlay.width, overlay.width * 2, step_x):
            draw.text((x, y), label, fill=(80, 80, 80, 62), font=font)
    overlay = overlay.rotate(28, resample=Image.Resampling.BICUBIC, expand=False)
    left = (overlay.width - result.width) // 2
    top = (overlay.height - result.height) // 2
    overlay = overlay.crop((left, top, left + result.width, top + result.height))
    return Image.alpha_composite(result.convert("RGBA"), overlay).convert("RGB")


def export_image(page: Page, output: str | Path, purpose: str = "", date_text: str = "") -> None:
    image = apply_redactions(page.image, page.redactions)
    image = add_watermark(image, purpose, date_text)
    target = Path(output)
    fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".bmp": "BMP"}.get(target.suffix.lower(), "PNG")
    if fmt == "JPEG":
        image.save(target, format=fmt, quality=95, optimize=True)
    else:
        image.save(target, format=fmt)


def export_pdf(pages: list[Page], output: str | Path, purpose: str = "", date_text: str = "") -> None:
    """Create a fresh image-only PDF. No source text or metadata is copied."""
    result = fitz.open()
    for page_record in pages:
        image = apply_redactions(page_record.image, page_record.redactions)
        image = add_watermark(image, purpose, date_text)
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        page_width, page_height = page_record.page_size_pt or (
            image.width * 72.0 / 150.0, image.height * 72.0 / 150.0
        )
        page = result.new_page(width=page_width, height=page_height)
        page.insert_image(page.rect, stream=buffer.getvalue())
    result.set_metadata({
        "title": "Redacted document", "author": "Local Redactor",
        "subject": "Flattened redacted export", "keywords": "",
        "creator": "Local Redactor", "producer": "Local Redactor",
        "creationDate": "", "modDate": "",
    })
    result.save(str(output), garbage=4, deflate=True, clean=True)
    result.close()
