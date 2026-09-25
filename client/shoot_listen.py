"""shoot_listen.py — PC worker: OCR local + trigger para la tablet.
- GET /ping -> "ok" instantáneo (la Pi detecta PC on/off en ~1s).
- POST /ocr (multipart file) -> texto RapidOCR local PC (rápido, full-screen).
- POST /capturar -> captura y POST a la Pi (botón 📸 tablet, ACK inmediato).
Solo stdlib + mss/pillow/requests (+ rapidocr-onnxruntime para /ocr).
?key= obligatorio si RTJPN_PC_KEY definido.
"""
import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import mss
import requests
from PIL import Image
import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import mss
import requests
from PIL import Image

_RAPID = None
_RAPID_PHOTO = None
_MODEL_DIR = os.path.join(os.path.expanduser("~"), ".rtjpn_models")
_REC_URL = ("https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/"
            "v3.9.2/onnx/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile.onnx")
_DICT_URL = ("https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/"
             "v3.9.2/paddle/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile/japan_dict.txt")


def _dl(url: str, path: str, min_bytes: int) -> str:
    import urllib.request
    if os.path.exists(path) and os.path.getsize(path) > min_bytes:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    urllib.request.urlretrieve(url, tmp)
    if os.path.getsize(tmp) <= min_bytes:
        raise RuntimeError(f"bad download: {url}")
    os.replace(tmp, path)
    return path


def _eng(photo: bool):
    global _RAPID, _RAPID_PHOTO
    if photo and _RAPID_PHOTO is None:
        from rapidocr_onnxruntime import RapidOCR
        rec = _dl(_REC_URL, os.path.join(_MODEL_DIR, "japan_rec.onnx"), 1_000_000)
        keys = _dl(_DICT_URL, os.path.join(_MODEL_DIR, "japan_dict.txt"), 1000)
        _RAPID_PHOTO = RapidOCR(rec_model_path=rec, rec_keys_path=keys,
                                det_box_thresh=0.3, det_limit_side_len=960)
    if not photo and _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        rec = _dl(_REC_URL, os.path.join(_MODEL_DIR, "japan_rec.onnx"), 1_000_000)
        keys = _dl(_DICT_URL, os.path.join(_MODEL_DIR, "japan_dict.txt"), 1000)
        _RAPID = RapidOCR(rec_model_path=rec, rec_keys_path=keys)
    return _RAPID_PHOTO if photo else _RAPID


def _photo_pre(img: Image.Image) -> Image.Image:
    from PIL import ImageFilter, ImageOps
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.LANCZOS)
    img = ImageOps.autocontrast(img.convert("RGB"), cutoff=2)
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=2))


def rapid_text(pil_img: Image.Image, photo: bool = False):
    """OCR RapidOCR local PC con rec JAPONÉS (default del paquete es chino).
    Devuelve (texto, conf_por_linea)."""
    import numpy as np
    img = _photo_pre(pil_img) if photo else pil_img.convert("RGB")
    res, _ = _eng(photo)(np.array(img))
    if not res:
        return "", []
    ordered = sorted(res, key=lambda b: b[0][0][1])
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

PORT = int(os.getenv("PC_LISTEN_PORT", "8120"))
RTJPN_PC_KEY = os.getenv("RTJPN_PC_KEY", "")
RTJPN_URL = os.getenv("RTJPN_URL", "http://192.168.10.10:8000/api/ocr")
RTJPN_KEY = os.getenv("RTJPN_KEY", "")
RTJPN_ROI = os.getenv("RTJPN_ROI", "")
RTJPN_BACKEND = os.getenv("RTJPN_BACKEND", "")


def shoot(backend: str = "") -> str:
    with mss.MSS() as sct:
        if RTJPN_ROI:
            x, y, w, h = map(int, RTJPN_ROI.split(","))
            area = {"left": x, "top": y, "width": w, "height": h}
        else:
            area = sct.monitors[1]
        raw = sct.grab(area)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
    params = {}
    if RTJPN_KEY:
        params["key"] = RTJPN_KEY
    be = backend or RTJPN_BACKEND
    if be:
        params["backend"] = be
    r = requests.post(RTJPN_URL, params=params or None,
                      files={"file": ("cap.jpg", buf.getvalue(), "image/jpeg")},
                      timeout=90)
    r.raise_for_status()
    return (r.json().get("text") or "(sin texto)")[:120]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, q):
        if RTJPN_PC_KEY and q.get("key", [""])[0] != RTJPN_PC_KEY:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ping":
            return self._json({"ok": True})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._auth(q):
            return
        if u.path == "/ocr":
            # OCR en PC: imagen -> texto (la Pi tokeniza/diccionario/broadcast)
            try:
                import cgi
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                        environ={"REQUEST_METHOD": "POST"})
                f = form["file"] if "file" in form else None
                data = f.file.read() if f is not None else self.rfile.read(
                    int(self.headers.get("Content-Length", 0)))
                img = Image.open(io.BytesIO(data)).convert("RGB")
                if max(img.size) > 1568:
                    img.thumbnail((1568, 1568), Image.LANCZOS)
                t0 = __import__("time").time()
                txt, confs = rapid_text(img, photo=q.get("photo", [""])[0] == "1")
                ms = int((__import__("time").time() - t0) * 1000)
                print(f"[{ms}ms] {(txt or '(empty)')[:100]}", flush=True)
                return self._json({"text": txt, "ms": ms, "line_conf": confs})
            except ImportError:
                return self._json({"error": "rapidocr no instalado en PC"}, 501)
            except Exception as e:
                print(f"ERR /ocr: {e}", flush=True)
                return self._json({"error": str(e)[:200]}, 500)
        if u.path != "/capturar":
            self.send_response(404)
            self.end_headers()
            return
        # ACK inmediato + proceso en background: el resultado vuelve por WS
        # a la tablet (si esperáramos al OCR, el proxy Pi daría timeout).
        import threading
        be = q.get("backend", [""])[0]
        print(f"capturar! backend={be or 'default'}", flush=True)
        threading.Thread(target=_bg, args=(be,), daemon=True).start()
        body = b"OK disparada"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _bg(backend: str):
    try:
        print(shoot(backend), flush=True)
    except Exception as e:
        print(f"ERR bg: {e}", flush=True)


if __name__ == "__main__":
    print(f"escuchando en :{PORT}/capturar (key={'sí' if RTJPN_PC_KEY else 'no'})")
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
