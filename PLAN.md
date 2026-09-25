# JPN-OCR — Plan de implementación paso a paso

Decisiones aprobadas: fugashi=unidic-lite, bot en Pi, glosas en inglés (JMdict nativo).
Cliente Win: AHK v2 trigger + shoot_once.py (reemplaza keyboard/pygame residente).

## Fase 0 — Base + red + disco (una vez)
- Pi OS Lite 64-bit, IP fija 192.168.10.10 + `raspberrypi.local`, preferible cable (WiFi 2.4GHz de la 3B+ es lento para PNG 3-5MB).
- `sudo apt install tesseract-ocr tesseract-ocr-jpn fonts-noto-cjk python3-pip python3-opencv`
  # NOTA: cv2 por apt (precompilado). NO usar pip opencv-python-headless en Pi 32-bit (compila 2h y falla).
- Verificar: `tesseract --list-langs | grep jpn`, `free -h`, `df -h /` (jamdict-data ~400MB + unidic-lite, SD 16GB mínima).
- Cliente comprime siempre a JPG q70 + downscale antes del POST (no PNG crudo).

## Fase 1 — server.py base + selftest OCR [ACTUAL]
- `server.py`: FastAPI + `preprocess()` + interfaz `ocr(image)->text` (backend inicial Tesseract, swappable a futuro por RapidOCR-ONNX sin reescribir: `OCR_BACKEND=tesseract|rapidocr`).
- Tesseract inicial: `lang=jpn+jpn_vert, --oem 1 --psm 6 con fallback a psm 5/11 para tategaki`.
- `preprocess()` definitivo (fusiona blind 1+2): gray → 2x LANCZOS → Otsu (`cv2.threshold+THRESH_OTSU`, invert si fondo oscuro) → fallback `adaptiveThreshold` para cajas semitransparentes → apertura morfológica `cv2.morphologyEx(OPEN, kernel 3x3-5x5 tunable)` para borrar furigana nativo (Animal Crossing). Flag `?keep_furigana=0/1`.
- Riesgo conocido: morfología puede comer `ゃゅっ、。` pequeños → kernel por juego en config.
- `import cv2` viene de `python3-opencv` por apt (ver Fase 0).
- Endpoints: `GET /`, `GET /api/health`, `GET /api/selftest`, `POST /api/ocr` (con lock asyncio: 1 OCR a la vez, evita OOM con 2 fotos seguidas; `workers=1`). `POST /api/ocr` acepta override `?roi=x,y,w,h&keep_furigana=0/1&psm=6` para ajuste fino remoto desde tablet/bot.
- `selftest`: genera imagen PIL con "日本語テスト" (Noto CJK 64px/80px) y se auto-OCRea. Sin PC/móvil.
- Checks: `curl localhost:8000/api/health`, `/api/selftest`, `POST /api/ocr -F file=@test.png`.
- Criterio salida: `selftest.ok=true` ("日本" en texto).

## Fase 2 — Lenguaje (fugashi + jamdict)
- `Tagger()` singleton (unidic-lite), `Jamdict()` singleton + `lru_cache(2000)`.
- `POST /api/ocr -> {text, tokens:[{surface,lemma,reading(hiragana via jaconv),pos,glosses}]}`.
- Máx 2 entradas / 4 senses por token para RAM (<350MB total).
- Nota furigana: si OCR escupe `わ私たし`, Fugashi no matchea → por eso la limpieza es en Fase 1, no aquí.
- Check: selftest con frase "食べる" devuelve lemma + glosses EN.

## Fase 3 — Tiempo real WS + frontend
- `WS /ws` + broadcast + `GET /api/last` + reconexión auto cada 2s + historial últimas 10 + `wake-lock` tablet.
- `static/index.html`: dark, táctil, `#sent` 2rem, `<ruby><rt>` furigana, card Yomitan (palabra+hiragana+glosas). PROHIBIDO romaji.
- Audio costo cero: `speechSynthesis` `lang="ja-JP"` en tablet (0 carga Pi), botón 🔊 por palabra y por oración (requiere tap, ya existe).
- Atribución obligatoria en footer: JMdict CC BY-SA (licencia diccionario).
- Check: abrir `http://pi:8000`, enviar OCR, ver furigana, popup y audio.

## Fase 4 — Cliente Windows (AHK v2 + shoot_once)
- `client.ahk` (v2, ~5MB, sin admin para hotkey global): `^+j` y `Joy1` como triggers → llama a `shoot_once.py`.
- `shoot_once.py` (sin loop, sin `keyboard`, sin `pygame`): `mss` + ROI configurable local `{"left,top,width,height"}` (caja de diálogo, clave para precisión) + JPG q70 + `POST http://192.168.10.10:8000/api/ocr?key=...&roi=...` → termina. ROI también ajustable remoto (ver Fase 1 override) y vía `/roi` del bot con foto recortada.
- `inputs` queda como plan B solo si se quiere 100% Python.
- `pip install mss pillow requests` + AHK v2 runtime.
- Check: captura → tablet muestra resultado.

## Fase 5 — Bot Telegram (en Pi)
- `telegram_bot.py`: `python-telegram-bot + httpx`, handlers `photo/start`.
- Flujo: foto → "📤 Enviada…" → POST 127.0.0.1:8000 → "✅ Traducida, mírala: http://192.168.10.10:8000".
- Dependencia internet: bot necesita salida a `api.telegram.org` siempre; web/tablet sigue offline, bot no. `systemd Restart=always`.
- Token vía @BotFather. Check: enviar foto → doble mensaje.

## Fase 6 — Deploy + docs
- systemd: `jpn-ocr.service` (uvicorn --workers 1) + `jpn-bot.service` + watchdog/restart.
- Seguridad LAN mínima: `?key=` compartido en `/api/ocr` + rate-limit simple (evita spam de vecinos que tumba la Pi).
- `requirements-pi.txt`, `requirements-win.txt`, `README.md`.
- Test punta a punta: juego → PC/bot → tablet.

## Estado (día 1: desplegado y validado en red real)
- [x] Fases 0-6 + dual-backend + rapidocr-JP + two-way PC-first + traducción EN + historial IndexedDB + UI EN
- [x] En Pi: systemd jpn-ocr + jpn-bot activos; health verde (rapidocr, groq:true, auth:true, pc:true)
- [x] Validado e2e: PC screenshot → [R] tablet; bot foto/archivo → ✅ + traducción; Tom Nook foto → 3 líneas + glosses + EN a nivel referencia
- [x] Display multilínea ({br:true} marcadores + <br/> render + 🔊 filtra marcadores)
- [ ] Audio 🔊 en Xiaomi Pad 2 (voz ja-JP) — prueba de usuario pendiente
- [ ] VPN para vista remota — opcional
