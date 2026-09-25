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


def rapid_text(pil_img: Image.Image) -> str:
    """OCR RapidOCR local PC (rápido). Requiere pip install rapidocr-onnxruntime."""
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        import numpy as np
        _RAPID = (RapidOCR(), np)
    eng, np = _RAPID
    res, _ = eng(np.array(pil_img.convert("RGB")))
    if not res:
        return ""
    lines = sorted(res, key=lambda b: b[0][0][1])
    return "\n".join(t for _, t, _ in lines if t).strip()

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
                return self._json({"text": rapid_text(img)})
            except ImportError:
                return self._json({"error": "rapidocr no instalado en PC"}, 501)
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        if u.path != "/capturar":
            self.send_response(404)
            self.end_headers()
            return
        # ACK inmediato + proceso en background: el resultado vuelve por WS
        # a la tablet (si esperáramos al OCR, el proxy Pi daría timeout).
        import threading
        be = q.get("backend", [""])[0]
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
