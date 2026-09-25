# RT-JPN-OCR

Real-time Japanese game translator: screenshot → OCR → furigana + JMdict popup dictionary. FastAPI server for Raspberry Pi 3B+ (1GB RAM), AHK screenshot client, Telegram bot, tap-to-hear tablet UI.

## Cómo funciona

```
PC (juego) ──screenshot──▶ Pi :8000 ──WS──▶ tablet (furigana + tarjeta + 🔊)
   ▲                          ▲
AHK/Joy1/button                │ foto Telegram (bypasea la PC)
tablet 📸 (vía proxy Pi) ──────┘
```

La Pi orquesta OCR (routing automático PC-first con fallback local;
motores `tesseract|rapidocr|groq`, pipeline foto dedicada con `?photo=1`),
tokeniza con `fugashi` + `unidic-lite`, busca glosses en JMdict (`jamdict`
SQLite local), traduce la oración a EN vía Groq texto (cascada, fase 2 por WS)
y lo emite por WebSocket. La tablet muestra el texto multilínea en grande
con `<ruby>` furigana + traducción itálica; cada palabra abre tarjeta estilo
Yomitan (palabra + hiragana + significados, **sin romaji**) con audio `ja-JP`
sintetizado en la tablet (cero carga Pi) e historial IndexedDB offline con
favoritas, orden y paginación. UI en inglés.

## Estructura

```
server.py              FastAPI + OCR triple + routing PC-first + WS + frontend
static/index.html      tablet UI (EN, dark, touch, no romaji)
client/shoot_once.py   on-demand capture PC (full-screen default, ROI opt, JPG q70)
client/shoot_listen.py PC worker :8120 (/ping + /ocr RapidOCR + /capturar)
client/client.ahk      AHK v2 triggers (J=default, T/R/G engines, Joy1)
client/USO_PC.md       PC guide
bot/telegram_bot.py    photo+document bot (/start, /roi, /modo) — runs on Pi
bot/USO_BOT.md         guía bot
deploy/*.service       systemd Pi
requirements-pi.txt    deps Pi (cv2 por apt, ver abajo)
```

## Pi 3B+ (una vez)

```bash
sudo apt update && sudo apt install -y python3-full python3-venv \
  python3-opencv tesseract-ocr tesseract-ocr-jpn fonts-noto-cjk
# cv2 por apt: pip compila 2h y falla en 32-bit. NO usar pip opencv.
# PEP 668: nada de pip al sistema; venv con acceso a los paquetes apt.
git clone https://github.com/anzorein/rt-jpn-ocr.git
cd rt-jpn-ocr
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements-pi.txt
cp .env.example .env   # completar claves, NUNCA commitear
.venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1
curl localhost:8000/api/selftest   # ok=true = base lista
```

## Variables (.env en la Pi, PC según caso)

| Var | Dónde | Default | Qué |
|---|---|---|---|
| `OCR_BACKEND` | Pi | `tesseract` | default `tesseract\|groq` |
| `GROQ_API_KEY` | Pi | — | cloud vision (sin esto, groq → 501) |
| `GROQ_MODEL` | Pi | llama-4-scout | modelo vision Groq |
| `API_KEY` | Pi | — | exige `?key=` en POST (vacío = abierto) |
| `RTJPN_PC_URL` | Pi | `http://192.168.10.15:8120/capturar` | proxy botón 📸 |
| `RTJPN_PC_KEY` | ambas | — | clave listener PC |
| `TELEGRAM_TOKEN` | Pi | — | token BotFather |
| `RTJPN_URL/KEY/ROI/BACKEND` | PC | — | ver `client/USO_PC.md` |

## Endpoints (Pi :8000)

- `/` tablet UI · `GET /api/health|last|selftest|parse?text=` · `WS /ws`
- `POST /api/ocr?backend=&roi=&psm=&keep_furigana=&key=` (multipart `file`)
- `POST /api/disparar?backend=&key=` (botón 📸 → PC)

## Backends OCR

| | tesseract (local) | rapidocr (local) | groq (cloud) |
|---|---|---|---|
| Internet | no | una vez (modelos) | sí |
| Latencia Pi 3B+ | 5-15 s (ROI) />60 s (full) | segundos (full-screen) | 2-4 s |
| Precisión juegos | ~60-70% | alta, texto en cualquier zona | ~90% |
| Costo/privacidad | gratis, local | gratis, local | API Groq, capturas a la nube |

Toggle: tablet T/R/G, PC `--backend`, bot `/modo` (precedencia: request > chat/PC > default).

## Deploy Pi (systemd)

```bash
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jpn-ocr jpn-bot
```

## Licencia

Código: MIT. Datos de diccionario: JMdict/EDRDG © CC BY-SA
(atribución en el footer de la tablet).
