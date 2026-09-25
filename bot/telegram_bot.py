"""telegram_bot.py — runs on the Pi next to the server.
Photo -> 📤 ack -> POST 127.0.0.1:8000 -> ✅ translated + tablet link.
Secrets ONLY via env (public repo): TELEGRAM_TOKEN, API_URL, WEB_URL.
/roi x,y,w,h — remote crop. /modo tesseract|rapidocr|groq — OCR engine.
Reply includes EN translation when the server provides one.
"""
import io
import os

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
API = os.getenv("API_URL", "http://127.0.0.1:8000/api/ocr")
API_KEY = os.getenv("API_KEY", "")  # mismo que el server (si auth:true, obligatorio)
WEB = os.getenv("WEB_URL", "http://192.168.10.10:8000/")
ROI = {"value": os.getenv("RTJPN_ROI", "")}
MODES = {}  # chat_id -> backend (default: servidor). /modo lo cambia.


async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Send me a photo of the dialogue 🎮\n"
        "Commands: /roi x,y,w,h (crop) — /roi off (full) — "
        "/modo tesseract|rapidocr|groq (OCR engine)")


async def modo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    arg = (u.message.text or "").replace("/modo", "").strip().lower()
    if arg in ("tesseract", "rapidocr", "groq"):
        MODES[u.effective_chat.id] = arg
        await u.message.reply_text(f"Engine: {arg}")
    else:
        cur = MODES.get(u.effective_chat.id, "server default")
        await u.message.reply_text(
            f"Current engine: {cur}\nUsage: /modo tesseract|rapidocr|groq")


async def roi(u: Update, c: ContextTypes.DEFAULT_TYPE):
    arg = (u.message.text or "").replace("/roi", "").strip()
    if arg.lower() in ("off", "no", ""):
        ROI["value"] = ""
        await u.message.reply_text("ROI cleared (full photo).")
    else:
        try:
            [int(x) for x in arg.split(",")]
            ROI["value"] = arg
            await u.message.reply_text(f"Remote ROI: {arg}")
        except Exception:
            await u.message.reply_text("Usage: /roi x,y,w,h — /roi off")


async def photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    f = await u.message.photo[-1].get_file()
    buf = io.BytesIO()
    await f.download_to_memory(buf)
    m1 = await u.message.reply_text("📤 Sent to server…")
    params = {}
    if API_KEY:
        params["key"] = API_KEY
    if ROI["value"]:
        params["roi"] = ROI["value"]
    if u.effective_chat.id in MODES:
        params["backend"] = MODES[u.effective_chat.id]
    try:
        async with httpx.AsyncClient(timeout=120) as h:
            r = await h.post(API, params=params or None,
                             files={"file": ("tg.jpg", buf.getvalue(), "image/jpeg")})
            r.raise_for_status()
            d = r.json()
        txt = (d.get("text") or "(no text)")[:400]
        n = len(d.get("tokens") or [])
        be = d.get("backend", "?")
        msg = (f"✅ Translated [{be} {d.get('ms', '?')}ms] ({n} words):\n{WEB}\n\n```{txt}```")
        await m1.edit_text(msg, parse_mode="Markdown")
        # Phase-2 translation lands seconds later: poll /api/last, match by text.
        tr = await _wait_translation(h, d.get("text", ""))
        if tr:
            try:
                await m1.edit_text(msg + f"\n\n_{tr}_", parse_mode="Markdown")
            except Exception:
                pass
    except Exception as e:
        await m1.edit_text(f"❌ Error: {e}")


async def _wait_translation(h: httpx.AsyncClient, text: str,
                            tries: int = 7) -> str | None:
    import asyncio
    base = API.rsplit("/api/ocr", 1)[0]
    for _ in range(tries):
        await asyncio.sleep(2)
        try:
            r = await h.get(base + "/api/last")
            d = r.json()
            if d.get("text") == text and d.get("translation"):
                return d["translation"]
        except Exception:
            pass
    return None


def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN (see .env.example)")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("roi", roi))
    app.add_handler(CommandHandler("modo", modo))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.run_polling()


if __name__ == "__main__":
    main()
