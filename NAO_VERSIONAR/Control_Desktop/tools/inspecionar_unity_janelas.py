import ctypes
from ctypes import wintypes, windll, byref
import psutil
import json

user32 = windll.user32
kernel32 = windll.kernel32

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

from remote_control_server import RECT, POINT

def inspecionar_unity():
    print("=== INSPECIONANDO PROCESSO E JANELAS DA UNITY ===")
    unity_procs = [p for p in psutil.process_iter(['pid', 'name']) if 'unity' in p.info['name'].lower()]
    print(f"Processos Unity encontrados: {len(unity_procs)}")
    for p in unity_procs:
        print(f"  PID {p.info['pid']}: {p.info['name']}")
        
    unity_pids = set()
    for p in unity_procs:
        if p.info['name'].lower() in ('unity.exe', 'unity hub.exe'):
            unity_pids.add(p.info['pid'])
            
    if not unity_pids:
        print("[ERRO] Nenhum processo da Unity encontrado.")
        return

    todas_janelas = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    
    def _enum_win(hwnd, lparam):
        pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value in unity_pids:
            visible = user32.IsWindowVisible(hwnd)
            title = get_window_text(hwnd)
            cls_name = get_class_name(hwnd)
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            r = RECT()
            user32.GetWindowRect(hwnd, byref(r))
            owner = user32.GetWindow(hwnd, GW_OWNER)
            enabled_popup = user32.GetWindow(hwnd, GW_ENABLEDPOPUP)
            
            todas_janelas.append({
                "hwnd": hwnd,
                "tid": tid,
                "title": title,
                "class": cls_name,
                "visible": bool(visible),
                "is_popup": bool(style & WS_POPUP),
                "is_child": bool(style & WS_CHILD),
                "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
                "owner": owner,
                "enabled_popup": enabled_popup
            })
        return True

    cb = WNDENUMPROC(_enum_win)
    # Abre o desktop correto
    hDesk = user32.OpenInputDesktop(0, False, 0x01FF)
    if hDesk:
        user32.SetThreadDesktop(hDesk)
        user32.EnumDesktopWindows(hDesk, cb, 0)
    else:
        user32.EnumWindows(cb, 0)

    print(f"\nTotal de janelas Win32 associadas à Unity: {len(todas_janelas)}")
    print("-" * 100)
    for w in todas_janelas:
        vis = "VISIVEL" if w["visible"] else "OCULTA "
        print(f"HWND: {w['hwnd']:8d} | {vis} | Class: {w['class']:25s} | Rect: {w['rect']} | Title: '{w['title']}'")
        print(f"    -> Owner HWND: {w['owner']} | GW_ENABLEDPOPUP: {w['enabled_popup']}")
    print("-" * 100)

    # Identificar a janela principal
    janela_principal = None
    for w in todas_janelas:
        if w["visible"] and w["rect"][2] > 400 and w["rect"][3] > 400:
            janela_principal = w
            break
            
    if janela_principal:
        h_main = janela_principal["hwnd"]
        print(f"\n[JANELA PRINCIPAL IDENTIFICADA] HWND: {h_main} ('{janela_principal['title']}')")
        popup_h = user32.GetWindow(h_main, GW_ENABLEDPOPUP)
        print(f"Chamada direta GetWindow(h_main, GW_ENABLEDPOPUP): {popup_h}")
        if popup_h == h_main:
            print("  => Retornou o PRÓPRIO h_main (nenhum popup ativo no momento).")
        else:
            print(f"  => Retornou um HWND DIFERENTE! HWND: {popup_h} ('{get_window_text(popup_h)}')")
            
    return todas_janelas

if __name__ == "__main__":
    from remote_control_server import run_in_desktop_thread
    run_in_desktop_thread(inspecionar_unity)
