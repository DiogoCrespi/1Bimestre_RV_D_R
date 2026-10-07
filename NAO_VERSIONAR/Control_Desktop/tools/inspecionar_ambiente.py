import psutil
import json
import ctypes
from ctypes import wintypes, windll

user32 = windll.user32
hDesk = user32.OpenInputDesktop(0, False, 0x01FF)
if hDesk:
    user32.SetThreadDesktop(hDesk)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
wins = []
def _enum(h, lparam):
    if user32.IsWindowVisible(h):
        len_txt = user32.GetWindowTextLengthW(h)
        if len_txt > 0:
            buf = ctypes.create_unicode_buffer(len_txt + 1)
            user32.GetWindowTextW(h, buf, len_txt + 1)
            rect = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(rect))
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            wins.append({
                'hwnd': h,
                'title': buf.value,
                'pid': pid.value,
                'rect': [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top]
            })
    return True

cb = WNDENUMPROC(_enum)
user32.EnumDesktopWindows(hDesk, cb, 0)

print(f"Total janelas encontradas no desktop: {len(wins)}")
for w in wins:
    try:
        proc = psutil.Process(w['pid']).name()
    except Exception:
        proc = "?"
    w['process'] = proc
    print(f"PID: {w['pid']:6d} | [{proc:18s}] | HWND: {w['hwnd']:8d} | {w['rect']} | {w['title']}")

with open("janelas_desktop.json", "w", encoding="utf-8") as f:
    json.dump(wins, f, indent=2, ensure_ascii=False)
