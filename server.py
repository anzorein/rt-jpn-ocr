"""Fase 3: FastAPI + Otsu + furigana + OCR swappable + fugashi/jamdict + WS + frontend."""
import asyncio
import io
import os
from functools import lru_cache
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Query, Request, WebSocket, WebSocketDisconnect
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

app = FastAPI(title="RT-JPN-OCR")
OCR_LOCK = asyncio.Lock()
OCR_BACKEND = os.getenv("OCR_BACKEND", "tesseract")  # default; override por ?backend=
API_KEY = os.getenv("API_KEY", "")  # vacío = sin auth (dev); en prod definir
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
PC_URL = os.getenv("PC_LISTENER_URL", "http://192.168.10.15:8120/capturar")
PC_KEY = os.getenv("PC_KEY", "")
BASE = Path(__file__).parent
LAST_RESULT: dict = {}
_RATE: dict = {}  # ip -> [timestamps] rate-limit simple anti-spam LAN


def gen_version() -> str:
    """Versión del generador (tokenizer+dict). El frontend marca
    entradas viejas como '↻ regenerable' si difiere."""
    parts = [OCR_BACKEND]
    try:
        import unidic_lite
        parts.append("unidic-lite-" + getattr(unidic_lite, "version", "?"))
    except Exception:
        parts.append("unidic-lite-?")
    try:
        import jamdict
        parts.append("jamdict-" + getattr(jamdict, "__version__", "?"))
    except Exception:
        parts.append("jamdict-?")
    return "|".join(parts)


def check_auth(key: str, request) -> None:
    from fastapi import HTTPException
    if API_KEY and key != API_KEY:
        raise HTTPException(401, "bad key")
    # rate-limit: 30 req/min por IP (evita que un vecino tumbe la Pi)
    import time
    ip = request.client.host if request.client else "?"
    now = time.time()
    lst = [t for t in _RATE.get(ip, []) if now - t < 60]
    if len(lst) >= 30:
        raise HTTPException(429, "rate limit")
    lst.append(now)
    _RATE[ip] = lst


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


GROQ_PROMPT = ("Transcribe ONLY the Japanese text visible in this videogame "
               "screenshot. Output the transcription and nothing else, preserving "
               "line breaks. Ignore small furigana readings above kanji, transcribe "
               "only the main text. If no Japanese text is visible, output nothing.")


async def ocr_groq(img: Image.Image) -> str:
    """Backend cloud (Groq vision). La Pi envía el JPG ya recortado.
    Sin GROQ_API_KEY -> 501, el modo local sigue andando."""
    from fastapi import HTTPException
    if not GROQ_KEY:
        raise HTTPException(501, "modo groq sin GROQ_API_KEY en la Pi")
    import base64
    import httpx
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode()
    async with httpx.AsyncClient(timeout=60) as h:
        r = await h.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={"model": GROQ_MODEL, "temperature": 0, "max_tokens": 1024,
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": GROQ_PROMPT},
                      {"type": "image_url",
                       "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


def ocr_dispatch(img: Image.Image, psm: int = 6) -> str:
    """Sync, solo tesseract (selftest + modo local). Groq es async aparte."""
    text = ocr_tesseract(img, psm)
    if not text and psm != 5:  # fallback tategaki
        text = ocr_tesseract(img, 5)
    return text


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
            "backend": OCR_BACKEND, "gen": gen_version(),
            "groq": bool(GROQ_KEY), "auth": bool(API_KEY)}


@app.get("/api/parse")
async def api_parse(text: str = Query(..., min_length=1, max_length=500)):
    """Tokeniza + diccionario sin OCR (útil para test sin tesseract).
    También usado por el botón ↻ regenerar del frontend."""
    toks = await asyncio.to_thread(tokenize, text)
    return {"text": text, "tokens": toks, "v": gen_version()}


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
    request: Request,
    file: UploadFile = File(...),
    keep_furigana: bool = Query(False),
    psm: int = Query(6),
    roi: str = Query("", description="x,y,w,h en px sobre imagen original"),
    backend: str = Query("", description="tesseract|groq (vacío=default)"),
    key: str = Query(""),
):
    check_auth(key, request)
    raw = await file.read()
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if roi:
        try:
            x, y, w, h = map(int, roi.split(","))
            img = img.crop((x, y, x + w, y + h))
        except Exception:
            pass
    be = (backend or OCR_BACKEND).lower()
    import time
    t0 = time.time()
    if be == "groq":
        # Cloud: imagen original a color (NO binarizada), downscale si enorme
        gimg = img.copy()
        if max(gimg.size) > 1568:
            gimg.thumbnail((1568, 1568), Image.LANCZOS)
        async with OCR_LOCK:
            text = await ocr_groq(gimg)
    elif be == "tesseract":
        proc = preprocess(img, keep_furigana=keep_furigana)
        async with OCR_LOCK:
            text = await asyncio.to_thread(ocr_dispatch, proc, psm)
    else:
        from fastapi import HTTPException
        raise HTTPException(400, f"backend desconocido: {be} (tesseract|groq)")
    ms = int((time.time() - t0) * 1000)
    toks = await asyncio.to_thread(tokenize, text) if text else []
    res = {"text": text, "tokens": toks, "v": gen_version(),
           "backend": be, "ms": ms}
    global LAST_RESULT
    LAST_RESULT = res
    await hub.broadcast(res)
    return res


@app.post("/api/disparar")
async def api_disparar(request: Request,
                       backend: str = Query(""),
                       key: str = Query("")):
    """Proxy botón 📸 tablet -> listener PC (captura) -> vuelve por /api/ocr."""
    from fastapi import HTTPException
    check_auth(key, request)
    import httpx
    params = {}
    if PC_KEY:
        params["key"] = PC_KEY
    if backend:
        params["backend"] = backend
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.post(PC_URL, params=params or None)
        return {"ok": r.status_code == 200, "pc": r.text[:200]}
    except Exception as e:
        raise HTTPException(502, f"PC no alcanzable ({PC_URL}): {e}")


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
