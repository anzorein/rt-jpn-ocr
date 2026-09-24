; client.ahk — AHK v2 trigger (~5MB RAM, hotkey global sin admin).
; J = default servidor | G = fuerza groq | T = fuerza local.
; Joystick botón 1 = default (el botón físico principal es el 📸 de la tablet;
; el Guide de Xbox One no es legible vía XInput estándar, por eso Joy1 genérico).
; Config por env: RTJPN_URL, RTJPN_KEY, RTJPN_ROI, RTJPN_BACKEND.
#Requires AutoHotkey v2.0
#SingleInstance Force

Shoot(extra := "") {
    ; Ajusta la ruta de python si usas venv
    Run('python "' A_ScriptDir '\shoot_once.py" ' extra, , "Hide")
}

; Hotkeys globales (sin admin para ventanas no elevadas)
^+j::Shoot()                 ; default (RTJPN_BACKEND o servidor)
^+g::Shoot("--backend groq") ; fuerza cloud
^+t::Shoot("--backend tesseract") ; fuerza local

; Joystick botón 1 por polling (sin foco, a nivel sistema)
SetTimer(JoyWatch, 100)
JoyWatch() {
    static prev := 0
    try {
        cur := GetKeyState("1Joy1")
        if (cur && !prev)
            Shoot()
        prev := cur
    }
}
