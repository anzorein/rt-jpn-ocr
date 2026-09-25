"""shoot_listen.py — listener LAN para el botón 📸 de la tablet.
Pi /api/disparar -> POST http://PC:8120/capturar?key=... -> captura y POST a la Pi.
Solo stdlib (+mss/pillow/requests ya instalados). ?key= obligatorio si RTJPN_PC_KEY definido.
Uso: set RTJPN_PC_KEY=... & set RTJPN_URL=http://192.168.10.10:8000/api/ocr & python shoot_listen.py
"""
import io
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import mss
import requests
from PIL import Image

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

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/capturar":
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(u.query)
        if RTJPN_PC_KEY and q.get("key", [""])[0] != RTJPN_PC_KEY:
            self.send_response(401)
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
