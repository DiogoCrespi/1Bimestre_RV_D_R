import time
import json
import psutil
import ctypes
from ctypes import wintypes
import statistics
import os
import sys

# Adicionar pasta raiz para imports auxiliares
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access, run_in_desktop_thread, focus_window,
    win32_mouse_move, user32
)
from testes_idxgi.idxgi_capture import DXGIOutputDuplicator, RECT

def calcular_intersecao_janela(dirty_rects, win_rect):
    """Calcula a área de dirty rects que interceptam a janela e a % da janela afetada."""
    if not win_rect or not dirty_rects:
        return 0, 0.0
    wx1, wy1 = win_rect["left"], win_rect["top"]
    wx2, wy2 = wx1 + win_rect["width"], wy1 + win_rect["height"]
    win_r = RECT(wx1, wy1, wx2, wy2)
    win_area = win_rect["width"] * win_rect["height"]
    if win_area <= 0:
        return 0, 0.0
        
    area_afetada = 0
    for dr in dirty_rects:
        r = RECT(dr[0], dr[1], dr[2], dr[3])
        inter = r.intersect(win_r)
        if inter:
            area_afetada += inter.area()
            
    pct_win = min(100.0, (area_afetada / win_area) * 100.0)
    return area_afetada, round(pct_win, 2)

def rodar_cenario(dupl, nome, duracao_s=2.5, intervalo_s=0.03, callback_acao=None, win_alvo=None):
    print(f"\n--- EXECUTANDO: {nome} ({duracao_s}s) ---")
    psutil.cpu_percent(interval=None) # reset cpu
    t_inicio = time.time()
    amostras = []
    cpu_leituras = []
    
    # Descartar frame inicial acumulado
    dupl.acquire_frame(timeout_ms=5)
    
    passo = 0
    while (time.time() - t_inicio) < duracao_s:
        passo += 1
        if callback_acao:
            callback_acao(passo)
            
        t0 = time.perf_counter()
        res = dupl.acquire_frame(timeout_ms=30)
        dt = (time.perf_counter() - t0) * 1000.0
        res["latencia_ms"] = round(dt, 2)
        
        if win_alvo:
            area_win, pct_win = calcular_intersecao_janela(res.get("dirty_rects", []), win_alvo)
            res["win_dirty_area"] = area_win
            res["win_dirty_pct"] = pct_win
            
        amostras.append(res)
        time.sleep(intervalo_s)
        
    cpu_uso = psutil.cpu_percent(interval=None)
    
    # Processar métricas do cenário
    total_frames = len(amostras)
    frames_com_update = [a for a in amostras if a.get("updated")]
    frames_timeout = [a for a in amostras if a.get("status") == "timeout"]
    frames_full = [a for a in frames_com_update if a.get("is_full_frame")]
    erros = [a for a in amostras if "error" in a.get("status", "")]
    
    latencias = [a["latencia_ms"] for a in amostras]
    dirty_counts = [a.get("dirty_count", 0) for a in frames_com_update]
    dirty_pcts = [a.get("dirty_pct_screen", 0.0) for a in frames_com_update]
    win_pcts = [a.get("win_dirty_pct", 0.0) for a in frames_com_update if "win_dirty_pct" in a]
    mouse_updates = sum(1 for a in amostras if a.get("pointer_updated"))
    
    resumo = {
        "cenario": nome,
        "total_coletas": total_frames,
        "frames_com_mudanca": len(frames_com_update),
        "frames_em_timeout": len(frames_timeout),
        "erros_access_lost": len(erros),
        "cpu_percent": cpu_uso,
        "latencia_media_ms": round(statistics.mean(latencias), 2) if latencias else 0,
        "latencia_p95_ms": round(statistics.quantiles(latencias, n=20)[18], 2) if len(latencias) >= 20 else 0,
        "fps_captura": round(total_frames / duracao_s, 1),
        "dirty_rects_medio": round(statistics.mean(dirty_counts), 2) if dirty_counts else 0,
        "dirty_rects_max": max(dirty_counts) if dirty_counts else 0,
        "dirty_pct_tela_media": round(statistics.mean(dirty_pcts), 2) if dirty_pcts else 0.0,
        "dirty_pct_tela_max": max(dirty_pcts) if dirty_pcts else 0.0,
        "pct_frames_100_dirty": round((len(frames_full) / max(1, len(frames_com_update))) * 100, 2),
        "mouse_pointer_updates": mouse_updates,
    }
    if win_pcts:
        resumo["janela_dirty_pct_media"] = round(statistics.mean(win_pcts), 2)
        resumo["janela_dirty_pct_max"] = max(win_pcts)
        resumo["janela_virou_full_count"] = sum(1 for p in win_pcts if p >= 98.0)
        
    print(f"  Resultados [{nome}]:")
    print(f"    Frames: {total_frames} ({resumo['fps_captura']} FPS) | Com mudança: {len(frames_com_update)} | Timeouts: {len(frames_timeout)}")
    print(f"    Latência média: {resumo['latencia_media_ms']}ms | CPU: {resumo['cpu_percent']}%")
    print(f"    Dirty rects/frame: {resumo['dirty_rects_medio']} (max {resumo['dirty_rects_max']})")
    print(f"    Área dirty tela: média {resumo['dirty_pct_tela_media']}% | max {resumo['dirty_pct_tela_max']}% | 100% full: {resumo['pct_frames_100_dirty']}%")
    if win_pcts:
        print(f"    Área dirty janela: média {resumo.get('janela_dirty_pct_media',0)}% | max {resumo.get('janela_dirty_pct_max',0)}% | Janela 100% dirty: {resumo.get('janela_virou_full_count',0)} vezes")
    print(f"    Pointer updates: {mouse_updates} | Erros/AccessLost: {len(erros)}")
    return resumo

def executar_bateria_completa():
    ensure_desktop_access()
    print("=" * 80)
    print("INICIANDO BATERIA DE BENCHMARK DO IDXGIOutputDuplication NO WINDOWS REAL")
    print("=" * 80)
    
    dupl = DXGIOutputDuplicator()
    relatorio = {"timestamp": time.time(), "cenarios": {}}
    
    try:
        # =====================================================================
        # 1. TELA COMPLETAMENTE PARADA (Repouso absoluto)
        # =====================================================================
        def _nada(p): pass
        r1 = rodar_cenario(dupl, "1_tela_completamente_parada", duracao_s=2.5, callback_acao=_nada)
        relatorio["cenarios"]["1_tela_parada"] = r1
        time.sleep(0.5)
        
        # =====================================================================
        # 2. CURSOR SE MOVENDO (Movimento puro de mouse)
        # =====================================================================
        def _mover_mouse(p):
            # Movimento em círculo/onda suave
            mx = 500 + int(300 * (p % 20) / 20.0)
            my = 400 + int(200 * (p % 10) / 10.0)
            win32_mouse_move(mx, my)
            
        r2 = rodar_cenario(dupl, "2_cursor_se_movendo", duracao_s=2.5, callback_acao=_mover_mouse)
        relatorio["cenarios"]["2_cursor_movimento"] = r2
        time.sleep(0.5)
        
        # =====================================================================
        # 3. TEXTO PISCANDO / CARET (Caret em campo de texto)
        # =====================================================================
        # Focar no Notepad ou janela de edição
        import tkinter as tk
        root = tk.Tk()
        root.title("CaretBenchmarkWin")
        root.geometry("300x150+100+100")
        entry = tk.Entry(root, font=("Consolas", 14))
        entry.pack(padx=20, pady=40)
        entry.focus_set()
        
        def _atualizar_tk(p):
            root.update()
            
        r3 = rodar_cenario(dupl, "3_texto_piscando_caret", duracao_s=2.5, callback_acao=_atualizar_tk)
        root.destroy()
        relatorio["cenarios"]["3_caret_piscando"] = r3
        time.sleep(0.5)
        
        # =====================================================================
        # 4. EXPLORER E JANELAS WIN32 COMUNS
        # =====================================================================
        ok_exp, win_exp = focus_window(process_name="explorer.exe")
        if not ok_exp or not win_exp:
            win_exp = {"left": 100, "top": 100, "width": 800, "height": 600}
            
        def _interagir_explorer(p):
            # Simular scroll suave ou hover no explorer
            if p % 4 == 0:
                win32_mouse_move(win_exp["left"] + 200 + (p * 5) % 100, win_exp["top"] + 200)
                
        r4 = rodar_cenario(dupl, "4_explorer_win32_comum", duracao_s=2.5, callback_acao=_interagir_explorer, win_alvo=win_exp)
        relatorio["cenarios"]["4_explorer"] = r4
        time.sleep(0.5)
        
        # =====================================================================
        # 5. UNITY EDITOR: UI ESTÁTICA / REPOUSO
        # =====================================================================
        ok_u, win_unity = focus_window(process_name="Unity.exe")
        if not ok_u or not win_unity:
            ok_u, win_unity = focus_window(title_kw="Unity")
            
        if win_unity:
            r5 = rodar_cenario(dupl, "5_unity_editor_estatico", duracao_s=2.5, callback_acao=_nada, win_alvo=win_unity)
            relatorio["cenarios"]["5_unity_estatico"] = r5
            time.sleep(0.5)
            
            # =====================================================================
            # 6. UNITY EDITOR: HOVER E ANIMAÇÃO PEQUENA NA UI
            # =====================================================================
            def _hover_unity(p):
                # Hover nos botões da barra superior (Play, Pause, Step) em (900..1000, 45)
                hx = win_unity["left"] + 880 + (p * 8) % 150
                hy = win_unity["top"] + 45
                win32_mouse_move(hx, hy)
                
            r6 = rodar_cenario(dupl, "6_unity_hover_animacao_ui", duracao_s=2.5, callback_acao=_hover_unity, win_alvo=win_unity)
            relatorio["cenarios"]["6_unity_hover_ui"] = r6
            time.sleep(0.5)
            
            # =====================================================================
            # 7. UNITY EDITOR: VIEWPORT / CENA EM MOVIMENTO
            # =====================================================================
            # Clicar e arrastar levemente no Scene view (x=700, y=400)
            def _arraste_scene_unity(p):
                if p == 1:
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0) # left down
                elif p == 25:
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0) # left up
                else:
                    win32_mouse_move(win_unity["left"] + 600 + (p * 4) % 100, win_unity["top"] + 350 + (p * 3) % 80)
                    
            r7 = rodar_cenario(dupl, "7_unity_viewport_movimento", duracao_s=2.5, callback_acao=_arraste_scene_unity, win_alvo=win_unity)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0) # garantir mouse solto
            relatorio["cenarios"]["7_unity_viewport"] = r7
            time.sleep(0.5)
            
        # =====================================================================
        # 8. TROCA DE JANELA, MINIMIZAR / RESTAURAR E REDIMENSIONAR
        # =====================================================================
        def _troca_janela(p):
            if p == 5 and win_unity:
                user32.ShowWindow(win_unity["hwnd"], 6) # SW_MINIMIZE
            elif p == 15 and win_unity:
                user32.ShowWindow(win_unity["hwnd"], 9) # SW_RESTORE
                
        r8 = rodar_cenario(dupl, "8_troca_janela_minimizar_restaurar", duracao_s=2.5, callback_acao=_troca_janela, win_alvo=win_unity)
        relatorio["cenarios"]["8_transicao_janela"] = r8
        
    finally:
        dupl.release()
        
    # Salvar relatório JSON completo
    caminho_json = os.path.join(os.path.dirname(__file__), "relatorio_idxgi.json")
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    print("\n" + "=" * 80)
    print(f"BATERIA COMPLETA CONCLUÍDA! Relatório salvo em: {caminho_json}")
    print("=" * 80)
    return relatorio

if __name__ == "__main__":
    import threading
    from remote_control_server import release_desktop_access
    res = []
    err = []
    def _t_runner():
        ensure_desktop_access()
        try:
            res.append(executar_bateria_completa())
        except Exception as e:
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_t_runner, daemon=True)
    t.start()
    t.join(timeout=180)
    if err:
        raise err[0]
