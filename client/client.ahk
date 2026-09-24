; client.ahk — AHK v2 trigger (~5MB RAM, hotkey global sin admin).
; Ctrl+Shift+J o botón 1 del joystick -> llama a shoot_once.py on-demand.
; Config por env: RTJPN_URL, RTJPN_KEY, RTJPN_ROI (x,y,w,h caja de diálogo).
#Requires AutoHotkey v2.0
#SingleInstance Force

Shoot() {
    ; Ajusta la ruta de python si usas venv
    Run('python "' A_ScriptDir '\shoot_once.py"', , "Hide")
}

; Hotkey global (sin admin para ventanas no elevadas)
^+j::Shoot()

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
