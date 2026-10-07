import json
from remote_control_server import get_open_windows, run_in_desktop_thread

def listar():
    wins = get_open_windows()
    relevantes = []
    for w in wins:
        if w.get('visible') and w.get('width', 0) > 200 and w.get('height', 0) > 200:
            title = str(w.get('title') or '')
            proc = str(w.get('process') or '')
            relevantes.append({
                'hwnd': w['hwnd'],
                'process': proc,
                'title': title,
                'rect': [w['left'], w['top'], w['width'], w['height']]
            })
            print(f"[{proc}] HWND: {w['hwnd']} | Rect: {w['left']},{w['top']} {w['width']}x{w['height']} | Title: {title[:70]}")
    with open('janelas_ativas.json', 'w', encoding='utf-8') as f:
        json.dump(relevantes, f, indent=2, ensure_ascii=False)

if __name__ == '__main__':
    run_in_desktop_thread(listar)
