"""Fase 1: FastAPI + preprocess Otsu + morfologia furigana + OCR swappable + selftest."""
import asyncio
import io
import os
from fastapi import FastAPI, UploadFile, File, Query
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import pytesseract
    HAS_TESS = True
except ImportError:
    HAS_TESS = False

app = FastAPI(title="JPN-OCR Fase1")
OCR_LOCK = asyncio.Lock()
OCR_BACKEND = os.getenv("OCR_BACKEND", "tesseract")


def preprocess(img: Image.Image, keep_furigana: bool = False):
    """gray -> 2x LANCZOS -> Otsu (+invert) -> OPEN para borrar furigana."""
    g = ImageOps.grayscale(img)
    w, h = g.size
    g = g.resize((w * 2, h * 2), Image.LANCZOS)
    if not HAS_CV2:
        return ImageOps.autocontrast(g, cutoff=1)
    arr = np.array(g)
    _, th = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Si fondo oscuro (esquina media oscura), invertir
    if float(th[:20, :20].mean()) < 127:
        th = cv2.bitwise_not(th)
    if not keep_furigana:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)
    return Image.fromarray(th)


def ocr_tesseract(img: Image.Image, psm: int = 6) -> str:
    if not HAS_TESS:
        return ""
    return pytesseract.image_to_string(
        img, lang="jpn+jpn_vert", config=f"--oem 1 --psm {psm}").strip()


def ocr_dispatch(img: Image.Image, psm: int = 6) -> str:
    # Punto de intercambio futuro: RapidOCR-ONNX sin reescribir el resto
    if OCR_BACKEND == "tesseract":
        text = ocr_tesseract(img, psm)
        if not text and psm != 5:  # fallback tategaki
            text = ocr_tesseract(img, 5)
        return text
    raise ValueError(f"backend desconocido: {OCR_BACKEND}")


def make_test_image(text: str = "日本語テスト") -> Image.Image:
    img = Image.new("RGB", (700, 180), "white")
    d = ImageDraw.Draw(img)
    font = None
    for p in ["C:/Windows/Fonts/msgothic.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"]:
        try:
            font = ImageFont.truetype(p, 72)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    d.text((30, 40), text, fill="black", font=font)
    return img


@app.get("/api/health")
async def health():
    return {"ok": True, "cv2": HAS_CV2, "tesseract": HAS_TESS,
            "backend": OCR_BACKEND}


@app.get("/api/selftest")
async def selftest():
    """Auto-verificacion sin imagen externa: genera imagen y se auto-OCRea."""
    img = make_test_image()
    proc = preprocess(img)
    async with OCR_LOCK:
        text = await asyncio.to_thread(ocr_dispatch, proc)
    return {"ok": "日本" in text, "text": text,
            "note": "tesseract no instalado" if not HAS_TESS else ""}


@app.post("/api/ocr")
async def api_ocr(
    file: UploadFile = File(...),
    keep_furigana: bool = Query(False),
    psm: int = Query(6),
    roi: str = Query("", description="x,y,w,h en px sobre imagen original"),
):
    raw = await file.read()
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if roi:
        try:
            x, y, w, h = map(int, roi.split(","))
            img = img.crop((x, y, x + w, y + h))
        except Exception:
            pass
    proc = preprocess(img, keep_furigana=keep_furigana)
    async with OCR_LOCK:
        text = await asyncio.to_thread(ocr_dispatch, proc, psm)
    return {"text": text}


@app.get("/", response_class=HTMLResponse)
async def index():
    return """<h1>JPN-OCR Fase1 OK</h1>
<p><a href="/api/health">health</a> - <a href="/api/selftest">selftest</a></p>"""
