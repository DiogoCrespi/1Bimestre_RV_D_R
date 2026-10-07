import subprocess
import time
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import ensure_desktop_access, focus_window, release_desktop_access
from testes_idxgi.idxgi_capture import DXGIOutputDuplicator
from testes_idxgi.benchmark_idxgi import rodar_cenario

def medir_chrome():
    ensure_desktop_access()
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "teste_animacao_video.html"))
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    
    print("\nIniciando Chrome com animação controlada...")
    proc = subprocess.Popen([chrome_path, f"--app=file:///{html_path}", "--window-size=800,600", "--window-position=100,100"])
    time.sleep(2.0)
    
    ok_c, win_c = focus_window(process_name="chrome.exe")
    if not ok_c or not win_c:
        ok_c, win_c = focus_window(title_kw="Teste de Dirty Rects")
        
    print(f"Janela Chrome detectada: HWND {win_c.get('hwnd')} | Rect: {win_c.get('width')}x{win_c.get('height')}")
    
    dupl = DXGIOutputDuplicator()
    try:
        def _nada(p): pass
        res_chrome = rodar_cenario(
            dupl, "chrome_animacao_e_canvas",
            duracao_s=3.0, intervalo_s=0.03, callback_acao=_nada, win_alvo=win_c
        )
    finally:
        dupl.release()
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
            
    # Carregar relatório existente e mesclar
    caminho_json = os.path.join(os.path.dirname(__file__), "relatorio_idxgi.json")
    rel = {}
    if os.path.exists(caminho_json):
        with open(caminho_json, "r", encoding="utf-8") as f:
            rel = json.load(f)
            
    rel.setdefault("cenarios", {})["chrome_electron_animacao"] = res_chrome
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(rel, f, indent=2, ensure_ascii=False)
        
    print("\nMedição do Chrome concluída e mesclada em relatorio_idxgi.json")
    return res_chrome

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(medir_chrome())
        except Exception as e:
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(60)
    if err:
        raise err[0]
