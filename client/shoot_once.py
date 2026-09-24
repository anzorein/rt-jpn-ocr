"""shoot_once.py — captura on-demand (llamado por client.ahk).
Sin loop, sin listeners: captura -> JPG q70 -> POST -> termina.
Secretos solo por env/args (repo público): RTJPN_URL, RTJPN_KEY, RTJPN_ROI.
"""
import argparse
import io
import os
import sys

import mss
import requests
from PIL import Image


def parse_roi(s: str):
    x, y, w, h = map(int, s.split(","))
    return {"left": x, "top": y, "width": w, "height": h}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("RTJPN_URL", "http://192.168.10.10:8000/api/ocr"))
    ap.add_argument("--key", default=os.getenv("RTJPN_KEY", ""))
    ap.add_argument("--roi", default=os.getenv("RTJPN_ROI", ""),
                    help="x,y,w,h opcional (más rápido). Vacío = pantalla completa.")
    ap.add_argument("--q", type=int, default=70, help="calidad JPG (red 2.4GHz Pi: 70)")
    ap.add_argument("--monitor", type=int, default=1)
    ap.add_argument("--backend", default=os.getenv("RTJPN_BACKEND", ""),
                    help="tesseract|rapidocr|groq (vacío=default servidor)")
    ap.add_argument("--psm", type=int, default=0,
                    help="0=default servidor, 6 bloques, 7 línea única")
    ap.add_argument("--timeout", type=int, default=180,
                    help="timeout POST en segundos (Pi lenta: 180)")
    a = ap.parse_args()

    with mss.MSS() as sct:
        area = parse_roi(a.roi) if a.roi else sct.monitors[a.monitor]
        raw = sct.grab(area)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=a.q)
        buf.seek(0)

    params = {}
    if a.key:
        params["key"] = a.key
    if a.backend:
        params["backend"] = a.backend
    if a.psm:
        params["psm"] = a.psm
    try:
        r = requests.post(a.url, params=params or None,
                          files={"file": ("cap.jpg", buf.getvalue(), "image/jpeg")},
                          timeout=a.timeout)
        r.raise_for_status()
        d = r.json()
        be = d.get("backend", "?")
        bl = {"groq": "G", "rapidocr": "R", "tesseract": "T"}.get(be, "?")
        tag = f"[{bl} {d.get('ms', '?')}ms " \
              f"(ocr {d.get('ocr_ms', '?')} + dict {d.get('dict_ms', '?')})]"
        print(f"{tag} {(d.get('text') or '(sin texto)')[:200]}")
        return 0
    except Exception as e:
        print(f"ERR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
