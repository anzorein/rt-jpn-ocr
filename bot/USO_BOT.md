# Bot de Telegram — corre en la Pi (ver PLAN Fase 5)
# 1. Telegram -> @BotFather -> /newbot -> copia el token
# 2. En la Pi: export TELEGRAM_TOKEN="123:ABC..." (o .env, NUNCA commitear)
#    opcionales: API_URL (default 127.0.0.1:8000), WEB_URL (IP pública tablet),
#    RTJPN_ROI (x,y,w,h inicial)
# 3. python telegram_bot.py  (Fase 6: systemd jpn-bot.service)
# Flujo: foto -> "📤 Enviada…" -> POST al server -> "✅ Traducida + link"
# /roi x,y,w,h ajusta recorte remoto — /roi off lo quita.
# Nota: el bot necesita internet (api.telegram.org); la web/tablet sigue offline.
