"""RT-JPN-OCR: FastAPI + Otsu + furigana + OCR dual + fugashi/jamdict + WS."""
import asyncio
import io
import os
from functools import lru_cache
from pathlib import Path

try:  # .env local (manual runs); systemd usa EnvironmentFile — ambos valen
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

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
OCR_PSM = int(os.getenv("OCR_PSM", "6"))  # 7 rinde mejor en diálogos de 1 línea
OCR_UPSCALE = int(os.getenv("OCR_UPSCALE", "2"))  # 3 para texto chico en ROI
API_KEY = os.getenv("API_KEY", "")  # vacío = sin auth (dev); en prod definir
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
GROQ_TEXT_MODELS = [m.strip() for m in
                    os.getenv("GROQ_TEXT_MODELS",
                              "qwen/qwen3.8-27b,openai/gpt-oss-120b,"
                              "openai/gpt-oss-20b,llama-3.3-70b-versatile").split(",")
                    if m.strip()]
GROQ_TRANSLATE_PROMPT = os.getenv("GROQ_TRANSLATE_PROMPT") or (
    "Translate this Japanese text into natural, idiomatic English that "
    "sounds like something a native speaker would say — not word-for-word. "
    "Preserve subtle nuances (e.g. とか marking just one example among "
    "others); do not narrow the meaning. Output only the translation: ")
_TEXT_WINNER: str | None = None  # primer modelo que responde 200, se reutiliza
RTJPN_PC_URL = os.getenv("RTJPN_PC_URL", "http://192.168.10.15:8120/capturar")
RTJPN_PC_KEY = os.getenv("RTJPN_PC_KEY", "")


def _pc_base() -> str:
    return RTJPN_PC_URL.rsplit("/capturar", 1)[0].rstrip("/")


async def pc_alive(timeout: float = 1.5) -> bool:
    """Presence-check: ¿PC prendida? Rápido, sin OCR."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as h:
            r = await h.get(_pc_base() + "/ping")
            return r.status_code == 200
    except Exception:
        return False


async def pc_ocr(raw: bytes, photo: bool = False):
    """OCR en PC (RapidOCR local allá). Timeout amplio: si hay ping, hay espera.
    Devuelve (texto, conf_por_linea)."""
    import httpx
    params = {}
    if RTJPN_PC_KEY:
        params["key"] = RTJPN_PC_KEY
    if photo:
        params["photo"] = "1"
    async with httpx.AsyncClient(timeout=30) as h:
        r = await h.post(_pc_base() + "/ocr", params=params or None,
                         files={"file": ("cap.jpg", raw, "image/jpeg")})
        r.raise_for_status()
        d = r.json()
        return d.get("text", ""), d.get("line_conf")
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
_RAPID = None
_RAPID_PHOTO = None


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


RAPID_REC_URL = os.getenv(
    "RAPID_REC_URL",
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/"
    "v3.9.2/onnx/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile.onnx")
RAPID_DICT_URL = os.getenv(
    "RAPID_DICT_URL",
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/"
    "v3.9.2/paddle/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile/japan_dict.txt")


def _dl(url: str, path: Path, min_bytes: int = 1_000_000) -> Path:
    if path.exists() and path.stat().st_size > min_bytes:
        return path
    import urllib.request
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    urllib.request.urlretrieve(url, tmp)
    if tmp.stat().st_size <= min_bytes:
        raise RuntimeError(f"descarga incompleta: {url}")
    tmp.replace(path)
    return path


def get_rapid():
    """RapidOCR-ONNX lazy con rec JAPONÉS (default del paquete es chino).
    Descarga una vez (~10MB) a ./models. Requiere Pi OS 64-bit."""
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        mdir = BASE / "models"
        rec = _dl(RAPID_REC_URL, mdir / "japan_PP-OCRv4_rec_mobile.onnx")
        keys = _dl(RAPID_DICT_URL, mdir / "japan_dict.txt", min_bytes=1000)
        _RAPID = RapidOCR(rec_model_path=str(rec), rec_keys_path=str(keys))
    return _RAPID


def get_rapid_photo():
    """Variante sensible para fotos (TV/móvil): umbral de detección bajo
    + lado límite mayor (texto chico/borroso). Mismo rec japonés."""
    global _RAPID_PHOTO
    if _RAPID_PHOTO is None:
        from rapidocr_onnxruntime import RapidOCR
        mdir = BASE / "models"
        rec = _dl(RAPID_REC_URL, mdir / "japan_PP-OCRv4_rec_mobile.onnx")
        keys = _dl(RAPID_DICT_URL, mdir / "japan_dict.txt", min_bytes=1000)
        _RAPID_PHOTO = RapidOCR(rec_model_path=str(rec), rec_keys_path=str(keys),
                                det_box_thresh=0.3, det_limit_side_len=960)
    return _RAPID_PHOTO


def preprocess_photo(img: Image.Image) -> Image.Image:
    """Normaliza fotos (texto marrón/crema + blur + moiré) antes de RapidOCR:
    upscale 2x + contraste fuerte + unsharp. Polaridad: si el fondo es claro
    con texto claro (blanco sobre azul), invierte a oscuro-sobre-claro.
    Solo PIL (determinista)."""
    from PIL import ImageFilter, ImageStat
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.LANCZOS).convert("RGB")
    if not _ink_dark(img):
        img = ImageOps.invert(img)  # tinta clara sobre fondo oscuro → normaliza
    img = ImageOps.autocontrast(img, cutoff=2)
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=2))


def _ink_dark(img: Image.Image) -> bool:
    """Heurística barata: ¿la tinta es más oscura que el fondo?
    Compara media de bordes (fondo) vs percentil oscuro (tinta)."""
    g = ImageOps.grayscale(img)
    w, h = g.size
    border = [g.getpixel((x, y)) for x in range(0, w, max(1, w // 20))
              for y in (0, h - 1)] + \
             [g.getpixel((x, y)) for y in range(0, h, max(1, h // 20))
              for x in (0, w - 1)]
    bg = sum(border) / max(1, len(border))
    data = sorted(g.getdata())
    dark = data[len(data) // 20]
    return dark < bg - 20


def ocr_rapid(img: Image.Image, photo: bool = False):
    """Full-screen friendly: detección + reconocimiento en una pasada.
    photo=True usa engine sensible + preproceso de contraste (fotos TV/móvil).
    Devuelve (texto, conf_por_linea) para highlight de sospechosos."""
    eng = get_rapid_photo() if photo else get_rapid()
    if photo:
        img = preprocess_photo(img)
    if HAS_CV2:
        arr = np.array(img.convert("RGB"))
    else:
        import numpy as _np
        arr = _np.array(img.convert("RGB"))
    res, _ = eng(arr)
    if not res:
        return "", []
    ordered = sorted(res, key=lambda b: b[0][0][1])  # top-to-bottom
    texts, confs = [], []
    for _, t, s in ordered:
        if not t:
            continue
        texts.append(t)
        try:
            confs.append(round(float(s if not isinstance(s, (list, tuple)) else s[0]), 3))
        except Exception:
            confs.append(None)
    return "\n".join(texts).strip(), confs


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
    for li, line in enumerate(text.replace("\r", "").split("\n")):
        if li:
            toks.append({"br": True})
        for w in tagger(line):
            surf = w.surface
            try:
                lemma = w.feature.lemma or surf
            except Exception:
                lemma = surf
            # unidic trae sufijos tipo "テスト-test": normaliza a forma base
            if "-" in lemma:
                head, tail = lemma.split("-", 1)
                if head and tail.isascii():
                    lemma = head
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
    """gray -> Nx LANCZOS -> Otsu (+invert) -> OPEN para borrar furigana."""
    g = ImageOps.grayscale(img)
    w, h = g.size
    g = g.resize((w * OCR_UPSCALE, h * OCR_UPSCALE), Image.LANCZOS)
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
    from fastapi import HTTPException
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode()
    try:
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
    except httpx.HTTPStatusError as e:
        # 404 = model ID no disponible para la key; 401 = key inválida...
        detail = e.response.text[:300]
        raise HTTPException(502, f"groq {e.response.status_code}: {detail}")


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
            "groq": bool(GROQ_KEY), "auth": bool(API_KEY),
            "pc": await pc_alive()}


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
    backend: str = Query("", description="tesseract|rapidocr|groq (vacío=default)"),
    photo: bool = Query(False, description="True=foto TV/móvil: contraste+det sensible+fallback"),
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
    psm = psm if psm != 6 else OCR_PSM  # ?psm= explícito gana, si no env
    ocr_by = "pi"
    line_conf = None
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
    elif be == "rapidocr":
        # Routing PC-first con presence-check: ping 1.5s (barato) → si la PC
        # está, OCR allá (1-2s); si no, local Pi. Automático, sin toggle.
        rimg = img.copy()
        if max(rimg.size) > 1568:
            rimg.thumbnail((1568, 1568), Image.LANCZOS)
        ocr_by = "pi"
        if await pc_alive():
            buf = io.BytesIO()
            rimg.save(buf, "JPEG", quality=80)
            try:
                async with OCR_LOCK:
                    text, line_conf = await pc_ocr(buf.getvalue(), photo)
                ocr_by = "pc"
            except Exception:
                ocr_by = "pi"
        line_conf = None
        if ocr_by == "pi":
            try:
                async with OCR_LOCK:
                    text, line_conf = await asyncio.to_thread(
                        ocr_rapid, rimg, photo)
            except ImportError:
                from fastapi import HTTPException
                raise HTTPException(501, "modo rapidocr no instalado en la Pi "
                                         "(pip install rapidocr-onnxruntime, Pi OS 64-bit)")
        # Fallback foto: si RapidOCR casi no leyó (<10 chars), reintenta
        # Tesseract SIN morfología (no come kana chicos) y reporta ganador.
        if photo and len(text) < 10:
            proc = preprocess(img, keep_furigana=True)
            async with OCR_LOCK:
                t2 = await asyncio.to_thread(ocr_dispatch, proc, psm)
            if len(t2) > len(text):
                text, ocr_by = t2, "tesseract-fallback"
    else:
        from fastapi import HTTPException
        raise HTTPException(400, f"backend desconocido: {be} (tesseract|rapidocr|groq)")
    ocr_ms = int((time.time() - t0) * 1000)
    t1 = time.time()
    toks = await asyncio.to_thread(tokenize, text) if text else []
    dict_ms = int((time.time() - t1) * 1000)
    import time as _t
    rid = f"{int(_t.time()*1000)}"
    res = {"id": rid, "text": text, "tokens": toks, "v": gen_version(),
           "backend": be, "ocr_by": ocr_by,
           "ms": ocr_ms + dict_ms,
           "ocr_ms": ocr_ms, "dict_ms": dict_ms,
           "psm": psm if be == "tesseract" else None,
           "line_conf": line_conf,
           "translation": None}
    global LAST_RESULT
    LAST_RESULT = res
    await hub.broadcast(res)
    if text:
        # Fase 2 (no bloquea la lectura): traducción EN llega como update {id}.
        readings = {t.get("reading", "") for t in toks if t.get("reading")}
        asyncio.create_task(_translate_and_push(rid, text, readings))
    return res


async def _translate_and_push(rid: str, text: str, readings: set):
    t = await translate_en(text, readings)
    if t is None:
        return
    if LAST_RESULT.get("id") == rid:
        LAST_RESULT["translation"] = t
    await hub.broadcast({"id": rid, "translation": t})


def _is_hira(s: str) -> bool:
    return bool(s) and all("ぁ" <= c <= "ゖ" or c in "ー〜" for c in s)


def clean_for_translation(text: str, readings: set) -> str:
    """Quita líneas de solo-lectura (furigana OCR como líneas sueltas) del
    input del traductor. Conservador: solo hiragana puro ≤6 chars que ya
    aparece como (parte de una) lectura de otro token. Ej: たぬきち/かた/
    しま/なに/そうだん fuera; ありがとう (diálogo real) queda.
    Env GROQ_CLEAN_INPUT=0 lo desactiva."""
    if os.getenv("GROQ_CLEAN_INPUT", "1") != "1":
        return text
    joined = "".join(readings)
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        if (_is_hira(s) and 1 <= len(s) <= 6 and
                (s in joined or any(len(r) >= 2 and r in s for r in readings))):
            continue
        out.append(ln)
    return "\n".join(out).strip() or text


async def translate_en(text: str, readings: set | None = None) -> str | None:
    """JA→EN vía Groq texto (solo texto a la nube, nunca capturas).
    Cascada de modelos (el primero con 200 gana y se cachea).
    Sin key o todos fallan → None (la UI oculta la línea)."""
    global _TEXT_WINNER
    if not GROQ_KEY or not text:
        return None
    text = clean_for_translation(text, readings or set())
    if not text:
        return None
    import httpx
    order = ([_TEXT_WINNER] if _TEXT_WINNER else []) + \
        [m for m in GROQ_TEXT_MODELS if m != _TEXT_WINNER]
    for model in order:
        content = await translate_one(model, text)
        if content:
            _TEXT_WINNER = model
            return content
    return None


@app.get("/api/translate")
async def api_translate(text: str = Query(..., min_length=1, max_length=500),
                        model: str = Query("", description="modelo puntual (vacío=cascada)")):
    """Traduce bajo demanda. ?model= fuerza un modelo (para comparar)."""
    if model:
        if not GROQ_KEY:
            from fastapi import HTTPException
            raise HTTPException(501, "traducción no configurada (GROQ_API_KEY)")
        t = await translate_one(model.strip(), text)
        if t is None:
            from fastapi import HTTPException
            raise HTTPException(502, f"modelo {model} no respondió")
        return {"text": text, "translation": t, "model": model}
    t = await translate_en(text)
    if t is None:
        from fastapi import HTTPException
        raise HTTPException(501, "traducción no configurada (GROQ_API_KEY)")
    return {"text": text, "translation": t}


async def translate_one(model: str, text: str) -> str | None:
    """Un intento contra un modelo: contenido no vacío o None."""
    import httpx
    import sys as _sys
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}"},
                json={"model": model, "temperature": 0,
                      "max_tokens": 512,
                      "messages": [{"role": "user", "content":
                          GROQ_TRANSLATE_PROMPT + text}]})
            if r.status_code != 200:
                print(f"translate {model}: HTTP {r.status_code} "
                      f"{r.text[:120]}", flush=True, file=_sys.stderr)
                return None
            content = (r.json()["choices"][0]["message"].get("content")
                       or "").strip()
            return content or None
    except Exception as e:
        print(f"translate {model}: ERR {e}", flush=True, file=_sys.stderr)
        return None


@app.post("/api/correct")
async def api_correct(request: Request):
    """Corrección manual: {text, parent_id?} -> pipeline completo
    (tokenize + translate) como NUEVA versión (edited:true).
    Sin cirugía de merge: dictado, furigana e historial quedan consistentes."""
    from fastapi import HTTPException
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido")
    check_auth(body.get("key", ""), request)
    text = (body.get("text") or "").strip()
    parent = body.get("parent_id")
    if not text or len(text) > 2000:
        raise HTTPException(400, "text vacío o >2000 chars")
    import time as _t
    t0 = _t.time()
    toks = await asyncio.to_thread(tokenize, text)
    dict_ms = int((_t.time() - t0) * 1000)
    rid = f"{int(_t.time()*1000)}"
    res = {"id": rid, "text": text, "tokens": toks, "v": gen_version(),
           "backend": "manual", "ocr_by": "user",
           "ms": dict_ms, "ocr_ms": 0, "dict_ms": dict_ms,
           "psm": None, "line_conf": None,
           "edited": True, "parent": parent, "orig_text": None,
           "translation": None}
    global LAST_RESULT
    LAST_RESULT = res
    await hub.broadcast(res)
    readings = {t.get("reading", "") for t in toks if t.get("reading")}
    asyncio.create_task(_translate_and_push(rid, text, readings))
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
    if RTJPN_PC_KEY:
        params["key"] = RTJPN_PC_KEY
    if backend:
        params["backend"] = backend
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(RTJPN_PC_URL, params=params or None)
        return {"ok": r.status_code == 200, "pc": r.text[:200]}
    except Exception as e:
        raise HTTPException(502, f"PC no alcanzable ({RTJPN_PC_URL}): {e}")


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
