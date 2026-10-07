import time
import json
import ctypes
from ctypes import wintypes, windll, byref
from remote_control_server import (
    user32, kernel32, RECT, POINT, ensure_desktop_access,
    run_in_desktop_thread, focus_window, win32_mouse_click
)

GW_HWNDFIRST = 0
GW_HWNDLAST = 1
GW_HWNDNEXT = 2
GW_HWNDPREV = 3
GW_OWNER = 4
GW_CHILD = 5
GW_ENABLEDPOPUP = 6

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_DISABLED = 0x08000000

user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]

def get_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value

def get_window_text(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    return ""

def listar_janelas_processo(pid):
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    res = []
    def _cb(h, lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, byref(p))
        if p.value == pid:
            style = user32.GetWindowLongW(h, GWL_STYLE)
            r = RECT()
            user32.GetWindowRect(h, byref(r))
            res.append({
                "hwnd": int(h),
                "title": get_window_text(h),
                "class": get_class_name(h),
                "visible": bool(user32.IsWindowVisible(h)),
                "disabled": bool(style & WS_DISABLED),
                "is_popup": bool(style & WS_POPUP),
                "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
                "owner": int(user32.GetWindow(h, GW_OWNER) or 0)
            })
        return True
    cb = WNDENUMPROC(_cb)
    hDesk = user32.OpenInputDesktop(0, False, 0x01FF)
    if hDesk:
        user32.SetThreadDesktop(hDesk)
        user32.EnumDesktopWindows(hDesk, cb, 0)
    else:
        user32.EnumWindows(cb, 0)
    return res

def testar():
    ensure_desktop_access()
    print("=== TESTE RIGOROSO: GW_ENABLEDPOPUP E DIÁLOGOS NA UNITY ===")
    
    # 1. Localizar janela do Unity
    ok, win = focus_window(title_kw="Unity 6")
    if not ok or not win:
        ok, win = focus_window(process_name="Unity.exe")
        
    if not win:
        print("[ERRO] Janela do Unity não encontrada!")
        return
        
    unity_hwnd = win["hwnd"]
    unity_pid = win["pid"]
    print(f"[OK] Unity Principal focado:")
    print(f"     HWND: {unity_hwnd}")
    print(f"     PID:  {unity_pid}")
    print(f"     Título: '{win['title']}'")
    
    # Estado inicial: sem diálogos abertos
    time.sleep(0.5)
    enabled_popup_inicial = user32.GetWindow(unity_hwnd, GW_ENABLEDPOPUP)
    print(f"\n1. Estado Inicial (Sem Diálogos):")
    print(f"   GetWindow(unity_hwnd, GW_ENABLEDPOPUP) = {enabled_popup_inicial}")
    if enabled_popup_inicial == unity_hwnd:
        print("   -> Retornou o próprio HWND principal (comportamento padrão Win32 quando NÃO há popup).")
    else:
        print(f"   -> Retornou HWND {enabled_popup_inicial}")

    janelas_antes = listar_janelas_processo(unity_pid)
    visiveis_antes = [w for w in janelas_antes if w["visible"]]
    print(f"   Janelas visíveis do processo Unity antes: {len(visiveis_antes)}")
    for w in visiveis_antes:
        print(f"     - HWND {w['hwnd']}: '{w['title']}' (Class: {w['class']}, Owner: {w['owner']})")

    # 2. Abrir um Diálogo no Unity via atalho de teclado: Ctrl+Shift+B (Build Settings)
    print(f"\n2. Abrindo diálogo do Unity (Build Settings via Ctrl+Shift+B)...")
    import pyautogui
    user32.SetForegroundWindow(unity_hwnd)
    time.sleep(0.3)
    pyautogui.hotkey('ctrl', 'shift', 'b')
    time.sleep(1.8) # Espera o Unity abrir a janela

    # 3. Inspecionar novamente após abrir o diálogo
    janelas_depois = listar_janelas_processo(unity_pid)
    visiveis_depois = [w for w in janelas_depois if w["visible"]]
    print(f"   Janelas visíveis do processo Unity depois: {len(visiveis_depois)}")
    
    novas_janelas = [w for w in visiveis_depois if w["hwnd"] not in [a["hwnd"] for a in visiveis_antes]]
    print(f"   Novas janelas Win32 que surgiram: {len(novas_janelas)}")
    for w in novas_janelas:
        print(f"     -> HWND {w['hwnd']}: '{w['title']}'")
        print(f"        Class: {w['class']} | Rect: {w['rect']} | Owner: {w['owner']} | Disabled: {w['disabled']}")
        
    enabled_popup_depois = user32.GetWindow(unity_hwnd, GW_ENABLEDPOPUP)
    print(f"\n   TESTE CHAVE GW_ENABLEDPOPUP:")
    print(f"   GetWindow(unity_hwnd, GW_ENABLEDPOPUP) = {enabled_popup_depois}")
    
    if enabled_popup_depois == unity_hwnd:
        print("   [ACHADO] GW_ENABLEDPOPUP retornou a janela principal (NÃO capturou como popup modal enabled).")
    elif enabled_popup_depois in [w["hwnd"] for w in novas_janelas]:
        print(f"   [ACHADO] GW_ENABLEDPOPUP CAPTUROU O DIÁLOGO! Retornou o HWND do diálogo: {enabled_popup_depois}")
    else:
        print(f"   [ACHADO] Retornou HWND: {enabled_popup_depois}")

    # Verificar se a janela principal ficou WS_DISABLED enquanto o diálogo está aberto
    style_main = user32.GetWindowLongW(unity_hwnd, GWL_STYLE)
    main_disabled = bool(style_main & WS_DISABLED)
    print(f"   A janela principal da Unity foi desabilitada (WS_DISABLED)?: {main_disabled}")

    # 4. Fechar a janela do Build Settings aberta (Escape ou fechar janela)
    print("\n3. Fechando o diálogo aberto...")
    if novas_janelas:
        for w in novas_janelas:
            user32.PostMessageW(w["hwnd"], 0x0010, 0, 0) # WM_CLOSE
    else:
        pyautogui.press('escape')
    time.sleep(0.8)

    # 5. Teste com Diálogo Nativo do Windows (File Explorer ou MessageBox) para comparação
    print("\n4. Comparação de Controle: Diálogo Modal Nativo Win32 (#32770)...")
    # Cria uma MessageBox em thread separada
    def _show_msg():
        user32.MessageBoxW(unity_hwnd, "Teste Modal", "Dialogo Teste", 0)
        
    import threading
    t_msg = threading.Thread(target=_show_msg, daemon=True)
    t_msg.start()
    time.sleep(0.5)
    
    popup_msg = user32.GetWindow(unity_hwnd, GW_ENABLEDPOPUP)
    print(f"   GetWindow(unity_hwnd, GW_ENABLEDPOPUP) com MessageBoxW nativa:")
    print(f"   Retornou: {popup_msg} (Título: '{get_window_text(popup_msg)}', Class: '{get_class_name(popup_msg)}')")
    
    # Fechar a MessageBox enviando enter
    pyautogui.press('enter')
    time.sleep(0.5)
    
    relatorio = {
        "unity_hwnd": unity_hwnd,
        "unity_title": win["title"],
        "popup_antes": enabled_popup_inicial,
        "novas_janelas_dialogo": novas_janelas,
        "popup_com_build_settings": enabled_popup_depois,
        "main_disabled_com_dialogo": main_disabled,
        "popup_com_messagebox_nativa": popup_msg
    }
    with open("relatorio_unity_gw_enabledpopup.json", "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    print("\n[SUCESSO] Relatório salvo em 'relatorio_unity_gw_enabledpopup.json'.")

if __name__ == "__main__":
    run_in_desktop_thread(testar)
