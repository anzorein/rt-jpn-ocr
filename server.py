"""Fase 3: FastAPI + Otsu + furigana + OCR swappable + fugashi/jamdict + WS + frontend."""
import asyncio
import io
import os
from functools import lru_cache
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
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

app = FastAPI(title="RT-JPN-OCR Fase3")
OCR_LOCK = asyncio.Lock()
OCR_BACKEND = os.getenv("OCR_BACKEND", "tesseract")
BASE = Path(__file__).parent
LAST_RESULT: dict = {}


class Hub:
    def __init__(self):
        self.clients: set = set()

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.clients.discard(d)


hub = Hub()

# Singletons perezosos (RAM Pi 3B+: no cargar dicts hasta primer uso)
_TAGGER = None
_JAM = None


def get_tagger():
    global _TAGGER
    if _TAGGER is None:
        from fugashi import Tagger
        _TAGGER = Tagger()  # unidic-lite
    return _TAGGER


def get_jam():
    global _JAM
    if _JAM is None:
        from jamdict import Jamdict
        _JAM = Jamdict()
    return _JAM


@lru_cache(maxsize=2000)
def lookup_cached(lemma: str):
    try:
        r = get_jam().lookup(lemma)
        out = []
        for e in r.entries[:2]:
            for s in e.senses[:4]:
                gloss = "; ".join(str(s).split(";")[:3]).strip()
                if gloss:
                    out.append(gloss)
                if len(out) >= 6:
                    break
        return out
    except Exception:
        return []


def tokenize(text: str):
    try:
        tagger = get_tagger()
    except Exception:
        return [{"surface": text, "lemma": text, "reading": "",
                 "pos": "", "glosses": []}] if text else []
    try:
        import jaconv
        kata2hira = jaconv.kata2hira
    except ImportError:
        kata2hira = lambda s: s
    toks = []
    for w in tagger(text.replace("\n", "")):
        surf = w.surface
        try:
            lemma = w.feature.lemma or surf
        except Exception:
            lemma = surf
        try:
            kana = w.feature.pron or w.feature.kana or surf
        except Exception:
            kana = surf
        try:
            hira = kata2hira(kana) if kana else surf
        except Exception:
            hira = surf
        try:
            pos = str(w.pos).split(",")[0]
        except Exception:
            pos = ""
        toks.append({"surface": surf, "lemma": lemma, "reading": hira,
                     "pos": pos,
                     "glosses": lookup_cached(lemma) if lemma else []})
    return toks


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


@app.get("/api/parse")
async def api_parse(text: str = Query(..., min_length=1, max_length=500)):
    """Tokeniza + diccionario sin OCR (útil para test sin tesseract)."""
    toks = await asyncio.to_thread(tokenize, text)
    return {"text": text, "tokens": toks}


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
    toks = await asyncio.to_thread(tokenize, text) if text else []
    res = {"text": text, "tokens": toks}
    global LAST_RESULT
    LAST_RESULT = res
    await hub.broadcast(res)
    return res


@app.get("/api/last")
async def api_last():
    return LAST_RESULT


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    if LAST_RESULT:
        try:
            await ws.send_json(LAST_RESULT)
        except Exception:
            pass
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.clients.discard(ws)


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")
