"""OpenCV/OCR-backed visual targeting.

Locates on-screen text (for web-view / custom-drawn content the UIA tree can't
expose) and maps it to absolute screen coordinates so the agent can click it.

Two pluggable OCR backends, auto-selected:
  * **tesseract** (via pytesseract + the Tesseract binary) — classical CV, fast
    on CPU (~1-5s); needs a one-time binary install. Preferred when present.
  * **rapidocr** (rapidocr-onnxruntime) — pip-only, no binary, but a deep-learning
    OCR that is slow on weak CPUs (tens of seconds on text-dense screens).

OCR only runs when the agent calls a visual tool, not every step.
"""

from __future__ import annotations

import difflib
import os
import shutil


# -- backend availability --------------------------------------------------
def _find_tesseract() -> str | None:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ):
        if os.path.isfile(p):
            return p
    return None


def _tesseract_ready() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    return _find_tesseract() is not None


_LANGS_CACHE: set | None = None


def _tesseract_langs() -> set:
    """Language packs installed alongside the Tesseract binary (probed once)."""
    global _LANGS_CACHE
    if _LANGS_CACHE is None:
        _LANGS_CACHE = set()
        exe = _find_tesseract()
        if exe:
            try:
                import subprocess

                out = subprocess.run(
                    [exe, "--list-langs"], capture_output=True, text=True, timeout=15
                ).stdout
                # First line is a header ("List of available languages...").
                _LANGS_CACHE = {ln.strip() for ln in out.splitlines()[1:] if ln.strip()}
            except Exception:
                pass
    return _LANGS_CACHE


def tesseract_lang() -> str:
    """OCR language string for the app under test.

    Tesseract reads only the languages it is told to. Running the English model
    over a Cyrillic UI returns mangled text ("Pa3gen" for "Раздел"), which makes
    `click_text` / `assert_text` miss on every non-ASCII label. Request the extra
    packs that are actually installed — asking for a missing one makes Tesseract
    fail outright, so unavailable packs are dropped. Override with
    APP_CLICKER_OCR_LANG (e.g. "rus+eng", "deu").
    """
    override = os.environ.get("APP_CLICKER_OCR_LANG", "").strip()
    installed = _tesseract_langs()
    if override:
        wanted = [p for p in override.split("+") if p in installed]
        return "+".join(wanted) if wanted else "eng"
    # Default: prefer the non-English packs the user installed, English last.
    extra = sorted(installed - {"eng", "osd"})
    return "+".join(extra + ["eng"]) if extra else "eng"


def _rapidocr_ready() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def active_engine() -> str | None:
    """Which OCR backend would be used ('tesseract' > 'rapidocr' > None)."""
    if _tesseract_ready():
        return "tesseract"
    if _rapidocr_ready():
        return "rapidocr"
    return None


def visual_available() -> bool:
    return active_engine() is not None


# -- backends --------------------------------------------------------------
class _TesseractBackend:
    name = "tesseract"

    def __init__(self):
        import pytesseract
        exe = _find_tesseract()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        self._pt = pytesseract
        self._lang = tesseract_lang()

    def recognize(self, pil_img):
        import cv2
        import numpy as np
        from pytesseract import Output

        gray = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)
        data = self._pt.image_to_data(gray, lang=self._lang, output_type=Output.DICT)

        # Group words into lines so multi-word labels ("Course Constructor") match.
        lines: dict = {}
        for i in range(len(data["text"])):
            txt = (data["text"][i] or "").strip()
            try:
                conf = float(data["conf"][i])
            except (ValueError, TypeError):
                conf = -1
            if not txt or conf <= 30:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x, y = data["left"][i], data["top"][i]
            w, h = data["width"][i], data["height"][i]
            e = lines.get(key)
            if e is None:
                lines[key] = {"words": [txt], "x0": x, "y0": y, "x1": x + w, "y1": y + h}
            else:
                e["words"].append(txt)
                e["x0"] = min(e["x0"], x)
                e["y0"] = min(e["y0"], y)
                e["x1"] = max(e["x1"], x + w)
                e["y1"] = max(e["y1"], y + h)

        out = []
        for e in lines.values():
            text = " ".join(e["words"])
            cx = (e["x0"] + e["x1"]) / 2.0
            cy = (e["y0"] + e["y1"]) / 2.0
            out.append((text, cx, cy))
        return out


class _RapidOcrBackend:
    name = "rapidocr"
    max_edge = 1000  # downscale before OCR; coords are rescaled back

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self._engine = RapidOCR()

    def recognize(self, pil_img):
        import cv2
        import numpy as np

        arr = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)
        h, w = arr.shape[:2]
        scale = 1.0
        if max(h, w) > self.max_edge:
            scale = self.max_edge / max(h, w)
            arr = cv2.resize(arr, (int(w * scale), int(h * scale)))
        result, _ = self._engine(arr)
        out = []
        for box, text, _score in (result or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = (min(xs) + max(xs)) / 2.0 / scale   # back to full-res window coords
            cy = (min(ys) + max(ys)) / 2.0 / scale
            out.append((text, cx, cy))
        return out


class VisualMatcher:
    def __init__(self, window, engine: str = "auto"):
        self.window = window
        self.engine = engine
        self._backend = None

    def _pick(self):
        if self._backend is not None:
            return self._backend
        want = self.engine
        if want in ("auto", "tesseract") and _tesseract_ready():
            self._backend = _TesseractBackend()
        elif want in ("auto", "rapidocr") and _rapidocr_ready():
            self._backend = _RapidOcrBackend()
        elif want == "tesseract":
            raise RuntimeError(
                "Tesseract not found. Install it (one-time):\n"
                "  winget install UB-Mannheim.TesseractOCR\n"
                "then reopen the shell."
            )
        elif want == "rapidocr":
            raise RuntimeError("rapidocr-onnxruntime is not installed (pip install rapidocr-onnxruntime==1.2.3).")
        else:
            raise RuntimeError(
                "No OCR backend available. Either install Tesseract "
                "(winget install UB-Mannheim.TesseractOCR) or pip install "
                "rapidocr-onnxruntime==1.2.3."
            )
        return self._backend

    def _capture(self):
        from PIL import ImageGrab

        rect = self.window.BoundingRectangle
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
        return img, bbox

    def ocr_words(self) -> tuple[list[dict], tuple]:
        img, bbox = self._capture()
        raw = self._pick().recognize(img)
        words = [
            {"text": t, "cx": cx, "cy": cy, "screen": (bbox[0] + cx, bbox[1] + cy)}
            for (t, cx, cy) in raw
        ]
        return words, bbox

    def find(self, query: str, words: list[dict]) -> list[dict]:
        q = query.lower().strip()
        exact = [w for w in words if q in w["text"].lower()]
        if exact:
            return exact
        scored = []
        for w in words:
            ratio = difflib.SequenceMatcher(None, q, w["text"].lower()).ratio()
            if ratio >= 0.6:
                scored.append((ratio, w))
        scored.sort(key=lambda t: -t[0])
        return [w for _, w in scored]
