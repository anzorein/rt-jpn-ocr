# Uso en PC Windows (cliente de captura)

## Instalar (una vez)

```powershell
cd D:\Documentos\Projects\IA\jpn-ocr\client
pip install -r requirements-win.txt
```

Opcional: AutoHotkey v2 desde https://www.autohotkey.com/
(solo para hotkey global + joystick; para probar basta el python).

## Configurar (una vez)

```powershell
$env:RTJPN_URL="http://192.168.1.50:8000/api/ocr"
```

Opcional pero recomendado — recorte a la caja de diálogo
(`x,y,w,h` en px; sin esto captura toda la pantalla y el OCR falla más):

```powershell
$env:RTJPN_ROI="100,700,1700,300"
```

Permanente: Panel de control → Sistema → Variables de entorno →
agregar `RTJPN_URL` (y `RTJPN_ROI`, `RTJPN_KEY` si usas).

## Usar

Prueba manual:

```powershell
python shoot_once.py
```

Jugando: doble-click a `client.ahk` (icono en bandeja) y luego
`Ctrl+Shift+J` o botón 1 del joystick.

El script imprime el texto en consola y la tablet lo muestra
con furigana + tarjeta + audio.

## Cómo sacar el ROI

1. Corre una vez sin `RTJPN_ROI` y mira qué lee.
2. Mide la caja de diálogo con la app Recortes
   (muestra coordenadas) o ajusta los 4 números a prueba y error.
3. Formato: `x,y,ancho,alto` (ej. `"100,700,1700,300"`).

## Backend local vs cloud

Default: lo que diga el servidor (`OCR_BACKEND`, `tesseract` = offline).
Override de un disparo:

```powershell
python shoot_once.py --backend groq
```

O pegajoso por env: `$env:RTJPN_BACKEND="groq"`.
Con AHK: `Ctrl+Shift+J` = default, `Ctrl+Shift+G` = groq, `Ctrl+Shift+T` = local.

## Botón 📸 de la tablet (listener)

Para disparar capturas desde la tablet con el mando en mano:

```powershell
$env:PC_KEY="una-clave-larga"   # compartida con la Pi (PC_KEY)
python shoot_listen.py          # escucha en :8120/capturar
```

La Pi lo llama vía `POST /api/disparar` (env `PC_LISTENER_URL` en la Pi).
Deja esta ventana abierta mientras juegas (o AHK para hotkeys).

## Notas

- Calidad JPG default 70 (`--q`): óptimo para el WiFi 2.4GHz de la Pi.
  `--q 85` si usas cable.
- Sin tesseract en esta PC verás `(sin texto)`: apunta a la Pi
  (`RTJPN_URL`) para resultados reales.
- Secretos por env, nunca en el código (repo público).
