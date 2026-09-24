"""telegram_bot.py — corre en la Pi junto al servidor.
Foto -> 📤 ack -> POST 127.0.0.1:8000 -> ✅ traducida + link tablet.
Secretos SOLO por env (repo público): TELEGRAM_TOKEN, API_URL, WEB_URL.
/roi x,y,w,h — ajusta recorte remoto (ver PLAN Fase 1 override).
"""
import io
import os

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
API = os.getenv("API_URL", "http://127.0.0.1:8000/api/ocr")
WEB = os.getenv("WEB_URL", "http://192.168.1.50:8000/")
ROI = {"value": os.getenv("RTJPN_ROI", "")}


async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Envíame una foto del diálogo 🎮\n"
        "Comandos: /roi x,y,w,h (recorte) — /roi off (quitar)")


async def roi(u: Update, c: ContextTypes.DEFAULT_TYPE):
    arg = (u.message.text or "").replace("/roi", "").strip()
    if arg.lower() in ("off", "no", ""):
        ROI["value"] = ""
        await u.message.reply_text("ROI quitado (foto completa).")
    else:
        try:
            [int(x) for x in arg.split(",")]
            ROI["value"] = arg
            await u.message.reply_text(f"ROI remoto: {arg}")
        except Exception:
            await u.message.reply_text("Uso: /roi x,y,w,h — /roi off")


async def photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    f = await u.message.photo[-1].get_file()
    buf = io.BytesIO()
    await f.download_to_memory(buf)
    m1 = await u.message.reply_text("📤 Enviada al servidor…")
    params = {}
    if ROI["value"]:
        params["roi"] = ROI["value"]
    try:
        async with httpx.AsyncClient(timeout=90) as h:
            r = await h.post(API, params=params or None,
                             files={"file": ("tg.jpg", buf.getvalue(), "image/jpeg")})
            r.raise_for_status()
            d = r.json()
        txt = (d.get("text") or "(sin texto)")[:400]
        n = len(d.get("tokens") or [])
        await m1.edit_text(
            f"✅ Traducida ({n} palabras), mírala aquí:\n{WEB}\n\n```{txt}```",
            parse_mode="Markdown")
    except Exception as e:
        await m1.edit_text(f"❌ Error: {e}")


def main():
    if not TOKEN:
        raise SystemExit("Define TELEGRAM_TOKEN (ver .env.example)")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("roi", roi))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.run_polling()


if __name__ == "__main__":
    main()
