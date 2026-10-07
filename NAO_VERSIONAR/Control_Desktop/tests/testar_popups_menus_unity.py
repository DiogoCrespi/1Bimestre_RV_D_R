import time
import ctypes
from ctypes import wintypes, windll, byref
from remote_control_server import (
    user32, kernel32, RECT, POINT, ensure_desktop_access,
    run_in_desktop_thread, focus_window, win32_mouse_click, win32_mouse_move
)

GW_OWNER = 4
GW_ENABLEDPOPUP = 6
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]

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

def testar_context_menu():
    ensure_desktop_access()
    print("=== TESTANDO POPUPS DE MENU E DIÁLOGOS DE CONFIRMAÇÃO NO UNITY ===")
    ok, win = focus_window(title_kw="Unity 6")
    if not ok:
        ok, win = focus_window(process_name="Unity.exe")
        
    unity_hwnd = win["hwnd"]
    pid = win["pid"]
    print(f"Unity HWND: {unity_hwnd}")
    
    # 1. Clicar com botão direito na área de Hierarchy para abrir Context Menu (Create/Paste...)
    # Vamos clicar em um ponto da janela principal (ex: 200, 200) com botão direito
    import pyautogui
    pyautogui.rightClick(win["left"] + 200, win["top"] + 200)
    time.sleep(0.6)
    
    # Inspecionar janelas abertas
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    popups = []
    def _cb(h, lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            owner = int(user32.GetWindow(h, GW_OWNER) or 0)
            popups.append({
                "hwnd": int(h),
                "title": get_window_text(h),
                "class": get_class_name(h),
                "owner": owner
            })
        return True
    cb = WNDENUMPROC(_cb)
    hDesk = user32.OpenInputDesktop(0, False, 0x01FF)
    user32.EnumDesktopWindows(hDesk, cb, 0)
    
    print(f"\nJanelas visíveis após abrir Menu de Contexto:")
    for p in popups:
        print(f"  -> HWND {p['hwnd']}: Class '{p['class']}', Title '{p['title']}', Owner: {p['owner']}")
        
    gw_popup = user32.GetWindow(unity_hwnd, GW_ENABLEDPOPUP)
    print(f"\nGetWindow(unity_hwnd, GW_ENABLEDPOPUP) com Menu de Contexto aberto: {gw_popup}")
    
    # Fechar menu com escape
    pyautogui.press('escape')
    time.sleep(0.3)

if __name__ == "__main__":
    run_in_desktop_thread(testar_context_menu)
