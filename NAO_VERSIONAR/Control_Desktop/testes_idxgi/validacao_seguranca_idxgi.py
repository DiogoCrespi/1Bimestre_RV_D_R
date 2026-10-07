import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time
import json
import logging
import ctypes
from ctypes import wintypes
import psutil
import numpy as np

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    win32_mouse_move,
    win32_mouse_down,
    win32_mouse_up,
    win32_mouse_scroll,
    win32_coords_to_normalized
)
from testes_idxgi.idxgi_capture import DXGIOutputDuplicator, RECT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("validacao_seguranca_idxgi")

import win32gui
user32 = ctypes.windll.user32

def obter_rect_janela(hwnd):
    try:
        if hwnd and win32gui.IsWindow(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            return list(rect)
    except Exception:
        pass
    return None

def focar_janela(hwnd):
    try:
        user32.ShowWindow(hwnd, 9) # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.3)
    except Exception:
        pass

def calcular_diff_ground_truth(prev_frame, curr_frame, union_rects, dirty_rects, win_rect=None, threshold=4):
    """
    Compara o frame anterior com o atual pixel a pixel.
    Calcula:
      - Pixels com mudança estrita (diff > 0)
      - Pixels com mudança perceptível (diff >= threshold)
      - Falsos Negativos com união (Dirty + Move): pixels alterados mas fora da máscara de união
      - Falsos Negativos usando apenas Dirty: pixels alterados mas fora da máscara de dirty
      - Falsos Positivos: regiões indicadas pelo DXGI onde os pixels não mudaram
      - Mesmas métricas isoladas na janela-alvo (se win_rect fornecido)
    """
    H, W, _ = curr_frame.shape
    diff_map = np.max(np.abs(curr_frame.astype(np.int16) - prev_frame.astype(np.int16)), axis=2)
    
    changed_strict = (diff_map > 0)
    changed_perceptual = (diff_map >= threshold)
    
    total_changed_perceptual = int(np.sum(changed_perceptual))
    total_changed_strict = int(np.sum(changed_strict))
    
    # Máscara da União (Dirty + Move Dst + Move Src)
    mask_union = np.zeros((H, W), dtype=bool)
    for r in union_rects:
        l = max(0, min(W, r[0]))
        t = max(0, min(H, r[1]))
        r_ = max(0, min(W, r[2]))
        b = max(0, min(H, r[3]))
        if r_ > l and b > t:
            mask_union[t:b, l:r_] = True
            
    # Máscara somente Dirty Rects (para comparar valor dos MoveRects)
    mask_dirty = np.zeros((H, W), dtype=bool)
    for r in dirty_rects:
        l = max(0, min(W, r[0]))
        t = max(0, min(H, r[1]))
        r_ = max(0, min(W, r[2]))
        b = max(0, min(H, r[3]))
        if r_ > l and b > t:
            mask_dirty[t:b, l:r_] = True

    # Métricas na Tela Inteira
    fn_union_perceptual = int(np.sum(changed_perceptual & (~mask_union)))
    fn_union_strict = int(np.sum(changed_strict & (~mask_union)))
    fn_dirty_only_perceptual = int(np.sum(changed_perceptual & (~mask_dirty)))
    
    fp_union_perceptual = int(np.sum((~changed_perceptual) & mask_union))
    tp_union_perceptual = int(np.sum(changed_perceptual & mask_union))
    
    # Métricas na Janela Alvo
    win_metrics = {}
    if win_rect:
        wl = max(0, min(W, win_rect[0]))
        wt = max(0, min(H, win_rect[1]))
        wr = max(0, min(W, win_rect[2]))
        wb = max(0, min(H, win_rect[3]))
        if wr > wl and wb > wt:
            win_changed_p = changed_perceptual[wt:wb, wl:wr]
            win_changed_s = changed_strict[wt:wb, wl:wr]
            win_mask_u = mask_union[wt:wb, wl:wr]
            win_mask_d = mask_dirty[wt:wb, wl:wr]
            
            w_total_p = int(np.sum(win_changed_p))
            w_fn_union_p = int(np.sum(win_changed_p & (~win_mask_u)))
            w_fn_dirty_p = int(np.sum(win_changed_p & (~win_mask_d)))
            w_fp_union_p = int(np.sum((~win_changed_p) & win_mask_u))
            w_tp_union_p = int(np.sum(win_changed_p & win_mask_u))
            
            win_metrics = {
                "win_pixels_changed_perceptual": w_total_p,
                "win_fn_union_perceptual": w_fn_union_p,
                "win_fn_dirty_only_perceptual": w_fn_dirty_p,
                "win_fp_union_perceptual": w_fp_union_p,
                "win_tp_union_perceptual": w_tp_union_p,
                "win_union_cobertura_pct": round((w_tp_union_p / w_total_p * 100.0) if w_total_p > 0 else 100.0, 2)
            }
            
    cobertura_tela_pct = round((tp_union_perceptual / total_changed_perceptual * 100.0) if total_changed_perceptual > 0 else 100.0, 2)
    
    return {
        "pixels_changed_strict": total_changed_strict,
        "pixels_changed_perceptual": total_changed_perceptual,
        "fn_union_perceptual": fn_union_perceptual,
        "fn_union_strict": fn_union_strict,
        "fn_dirty_only_perceptual": fn_dirty_only_perceptual,
        "fp_union_perceptual": fp_union_perceptual,
        "tp_union_perceptual": tp_union_perceptual,
        "cobertura_tela_pct": cobertura_tela_pct,
        "win": win_metrics
    }

def executar_cenario_validacao(dupl, nome, duracao_s=3.0, timeout_ms=25, acao_func=None, win_hwnd=None):
    """
    Executa uma rodada completa com captura síncrona de pixels,
    medindo latência pura de AcquireNextFrame, CPU do processo isolado,
    e validação de Ground Truth em cada frame.
    """
    logger.info(">>> INICIANDO CENÁRIO: %s (duração: %.1fs, timeout: %dms)", nome, duracao_s, timeout_ms)
    
    proc = psutil.Process(os.getpid())
    proc.cpu_percent(interval=None) # Reset CPU do processo isolado
    
    win_rect = obter_rect_janela(win_hwnd) if win_hwnd else None
    
    # Descartar frame anterior residual
    dupl.acquire_frame(timeout_ms=5)
    
    t_inicio = time.time()
    amostras_latencia = []
    total_coletas = 0
    total_timeouts = 0
    frames_com_mudanca = 0
    
    prev_frame = None
    metricas_gt = []
    
    # Iniciar ação em background se fornecida
    import threading
    acao_ativa = [True]
    if acao_func:
        def _th_acao():
            ensure_desktop_access()
            try:
                acao_func(acao_ativa)
            finally:
                release_desktop_access()
        t_acao = threading.Thread(target=_th_acao, daemon=True)
        t_acao.start()
        
    while (time.time() - t_inicio) < duracao_s:
        total_coletas += 1
        res = dupl.acquire_frame(timeout_ms=timeout_ms, capture_pixels=True)
        
        if res.get("is_timeout"):
            total_timeouts += 1
            continue
            
        if res.get("status") == "ok":
            frames_com_mudanca += 1
            lat = res["acquire_latency_ms"]
            if lat is not None:
                amostras_latencia.append(lat)
                
            curr_frame = res.get("pixels")
            if curr_frame is not None and prev_frame is not None:
                gt = calcular_diff_ground_truth(
                    prev_frame,
                    curr_frame,
                    res["union_rects"],
                    res["dirty_rects"],
                    win_rect=win_rect,
                    threshold=4
                )
                gt["dirty_count"] = res["dirty_count"]
                gt["move_count"] = res["move_count"]
                gt["union_count"] = res["union_count"]
                gt["dirty_pct_screen"] = res["dirty_pct_screen"]
                gt["is_full_frame"] = res["is_full_frame"]
                metricas_gt.append(gt)
                
            if curr_frame is not None:
                prev_frame = curr_frame
                
        time.sleep(0.01) # Pequena pausa entre coletas
        
    acao_ativa[0] = False
    
    # CPU isolada do processo
    process_cpu = proc.cpu_percent(interval=None)
    
    # Estatísticas de Latência pura (somente frames com sucesso)
    lat_media = round(float(np.mean(amostras_latencia)), 2) if amostras_latencia else 0.0
    lat_mediana = round(float(np.median(amostras_latencia)), 2) if amostras_latencia else 0.0
    lat_p95 = round(float(np.percentile(amostras_latencia, 95)), 2) if amostras_latencia else 0.0
    
    taxa_timeout_pct = round((total_timeouts / total_coletas * 100.0), 2) if total_coletas > 0 else 0.0
    fps_efetivo = round(frames_com_mudanca / duracao_s, 1)
    
    # Consolidação de Falsos Negativos e Ground Truth
    frames_com_mudanca_real = [m for m in metricas_gt if m["pixels_changed_perceptual"] > 0]
    total_frames_mudanca_real = len(frames_com_mudanca_real)
    
    total_fn_union = sum(m["fn_union_perceptual"] for m in frames_com_mudanca_real)
    total_fn_dirty_only = sum(m["fn_dirty_only_perceptual"] for m in frames_com_mudanca_real)
    total_pixels_mudados = sum(m["pixels_changed_perceptual"] for m in frames_com_mudanca_real)
    
    cobertura_global_union = round(
        ((total_pixels_mudados - total_fn_union) / total_pixels_mudados * 100.0) if total_pixels_mudados > 0 else 100.0,
        3
    )
    
    # Métricas da janela alvo
    win_frames = [m["win"] for m in frames_com_mudanca_real if m.get("win")]
    win_mudados = sum(w["win_pixels_changed_perceptual"] for w in win_frames)
    win_fn_union = sum(w["win_fn_union_perceptual"] for w in win_frames)
    win_fn_dirty = sum(w["win_fn_dirty_only_perceptual"] for w in win_frames)
    win_cobertura = round(
        ((win_mudados - win_fn_union) / win_mudados * 100.0) if win_mudados > 0 else 100.0,
        3
    )
    
    # Falsos positivos (área reportada vs pixels que realmente mudaram)
    total_fp_union = sum(m["fp_union_perceptual"] for m in frames_com_mudanca_real)
    
    # Move rects detectados
    total_move_rects = sum(m["move_count"] for m in metricas_gt)
    total_dirty_rects = sum(m["dirty_count"] for m in metricas_gt)
    
    resumo = {
        "cenario": nome,
        "duracao_s": duracao_s,
        "timeout_config_ms": timeout_ms,
        "total_coletas": total_coletas,
        "total_timeouts": total_timeouts,
        "taxa_timeout_pct": taxa_timeout_pct,
        "frames_com_mudanca": frames_com_mudanca,
        "fps_captura_efetivo": fps_efetivo,
        "process_cpu_percent": process_cpu,
        "latencia_pura_acquire_media_ms": lat_media,
        "latencia_pura_acquire_mediana_ms": lat_mediana,
        "latencia_pura_acquire_p95_ms": lat_p95,
        "total_dirty_rects": total_dirty_rects,
        "total_move_rects": total_move_rects,
        "frames_com_mudanca_pixel_real": total_frames_mudanca_real,
        "total_pixels_mudados": total_pixels_mudados,
        "fn_pixels_union": total_fn_union,
        "fn_pixels_dirty_only": total_fn_dirty_only,
        "fp_pixels_union": total_fp_union,
        "cobertura_pixels_union_pct": cobertura_global_union,
        "janela": {
            "hwnd": win_hwnd,
            "rect": win_rect,
            "pixels_mudados": win_mudados,
            "fn_pixels_union": win_fn_union,
            "fn_pixels_dirty_only": win_fn_dirty,
            "cobertura_janela_pct": win_cobertura
        }
    }
    
    logger.info("  [CONCLUÍDO %s]: Cobertura União: %s%% | FN União: %d | FN Dirty-Only: %d | MoveRects: %d | Latência: %.2fms | CPU Harness: %.1f%%",
                nome, cobertura_global_union, total_fn_union, total_fn_dirty_only, total_move_rects, lat_media, process_cpu)
    return resumo

def rodar_bateria_seguranca():
    ensure_desktop_access()
    dupl = DXGIOutputDuplicator()
    relatorio = {
        "timestamp": time.time(),
        "descricao": "Segunda Rodada de Validação IDXGI: Segurança de Percepção Incremental e Ground Truth",
        "cenarios": {}
    }
    
    # 1. Localizar Janelas
    hwnd_unity = 67032
    hwnd_chrome = 66970
    hwnd_explorer = 263316
    
    try:
        # =====================================================================
        # CENÁRIO 1: Mover uma Janela (Explorer)
        # =====================================================================
        focar_janela(hwnd_explorer)
        r_exp = obter_rect_janela(hwnd_explorer) or [100, 100, 800, 600]
        
        def _acao_mover_janela(ativa):
            tx = (r_exp[0] + r_exp[2]) // 2
            ty = r_exp[1] + 15 # Barra de título
            win32_mouse_move(tx, ty)
            time.sleep(0.1)
            win32_mouse_down("left", tx, ty)
            dx = 1
            while ativa[0]:
                tx += dx * 10
                if tx > r_exp[0] + 150 or tx < r_exp[0] - 50:
                    dx = -dx
                win32_mouse_move(tx, ty)
                time.sleep(0.04)
            win32_mouse_up("left", tx, ty)
            
        r1 = executar_cenario_validacao(dupl, "1_mover_janela_explorer", duracao_s=2.5, timeout_ms=25, acao_func=_acao_mover_janela, win_hwnd=hwnd_explorer)
        relatorio["cenarios"]["1_mover_janela_explorer"] = r1
        time.sleep(0.5)

        # =====================================================================
        # CENÁRIO 2: Scroll no Google Chrome
        # =====================================================================
        focar_janela(hwnd_chrome)
        r_chr = obter_rect_janela(hwnd_chrome) or [50, 50, 1000, 700]
        
        def _acao_scroll_chrome(ativa):
            cx = (r_chr[0] + r_chr[2]) // 2
            cy = (r_chr[1] + r_chr[3]) // 2
            win32_mouse_move(cx, cy)
            time.sleep(0.1)
            direcao = -120
            while ativa[0]:
                win32_mouse_scroll(direcao, 0, cx, cy)
                direcao = -direcao if time.time() % 1.2 > 0.6 else direcao
                time.sleep(0.08)
                
        r2 = executar_cenario_validacao(dupl, "2_scroll_chrome", duracao_s=3.0, timeout_ms=25, acao_func=_acao_scroll_chrome, win_hwnd=hwnd_chrome)
        relatorio["cenarios"]["2_scroll_chrome"] = r2
        time.sleep(0.5)

        # =====================================================================
        # CENÁRIO 3: Scroll no Explorer
        # =====================================================================
        focar_janela(hwnd_explorer)
        
        def _acao_scroll_explorer(ativa):
            cx = (r_exp[0] + r_exp[2]) // 2
            cy = (r_exp[1] + r_exp[3]) // 2
            win32_mouse_move(cx, cy)
            time.sleep(0.1)
            direcao = -120
            while ativa[0]:
                win32_mouse_scroll(direcao, 0, cx, cy)
                direcao = -direcao if time.time() % 1.0 > 0.5 else direcao
                time.sleep(0.08)

        r3 = executar_cenario_validacao(dupl, "3_scroll_explorer", duracao_s=2.5, timeout_ms=25, acao_func=_acao_scroll_explorer, win_hwnd=hwnd_explorer)
        relatorio["cenarios"]["3_scroll_explorer"] = r3
        time.sleep(0.5)

        # =====================================================================
        # CENÁRIO 4: Scroll e Movimentação na Unity (Console / Hierarchy)
        # =====================================================================
        focar_janela(hwnd_unity)
        r_uni = obter_rect_janela(hwnd_unity) or [0, 0, 1920, 1080]
        
        def _acao_scroll_unity(ativa):
            # Posicionar no quadrante inferior (tipicamente Project / Console)
            cx = r_uni[0] + 300
            cy = r_uni[1] + int((r_uni[3] - r_uni[1]) * 0.75)
            win32_mouse_move(cx, cy)
            time.sleep(0.1)
            direcao = -120
            while ativa[0]:
                win32_mouse_scroll(direcao, 0, cx, cy)
                direcao = -direcao if time.time() % 0.8 > 0.4 else direcao
                time.sleep(0.06)

        r4 = executar_cenario_validacao(dupl, "4_scroll_movimento_unity", duracao_s=3.0, timeout_ms=25, acao_func=_acao_scroll_unity, win_hwnd=hwnd_unity)
        relatorio["cenarios"]["4_scroll_movimento_unity"] = r4
        time.sleep(0.5)

        # =====================================================================
        # CENÁRIO 5: Arrastar Painéis / Splitter na Unity
        # =====================================================================
        focar_janela(hwnd_unity)
        
        def _acao_drag_splitter_unity(ativa):
            # Posição típica de divisão de abas/painéis na Unity
            cx = r_uni[0] + int((r_uni[2] - r_uni[0]) * 0.3)
            cy = r_uni[1] + int((r_uni[3] - r_uni[1]) * 0.5)
            win32_mouse_move(cx, cy)
            time.sleep(0.1)
            win32_mouse_down("left", cx, cy)
            offset = 1
            while ativa[0]:
                cx += offset * 8
                if cx > r_uni[0] + 500 or cx < r_uni[0] + 200:
                    offset = -offset
                win32_mouse_move(cx, cy)
                time.sleep(0.05)
            win32_mouse_up("left", cx, cy)

        r5 = executar_cenario_validacao(dupl, "5_drag_paineis_docking_unity", duracao_s=2.5, timeout_ms=25, acao_func=_acao_drag_splitter_unity, win_hwnd=hwnd_unity)
        relatorio["cenarios"]["5_drag_paineis_docking_unity"] = r5
        time.sleep(0.5)

        # =====================================================================
        # CENÁRIO 6: Maximizar, Restaurar e Redimensionar Janela
        # =====================================================================
        def _acao_transicao_janela(ativa):
            while ativa[0]:
                # Maximizar
                user32.ShowWindow(hwnd_explorer, 3) # SW_MAXIMIZE
                time.sleep(0.5)
                # Restaurar
                user32.ShowWindow(hwnd_explorer, 9) # SW_RESTORE
                time.sleep(0.5)
                # Redimensionar
                user32.SetWindowPos(hwnd_explorer, 0, r_exp[0] + 20, r_exp[1] + 20, 850, 650, 0x0040)
                time.sleep(0.5)
                user32.SetWindowPos(hwnd_explorer, 0, r_exp[0], r_exp[1], r_exp[2] - r_exp[0], r_exp[3] - r_exp[1], 0x0040)
                time.sleep(0.5)

        r6 = executar_cenario_validacao(dupl, "6_maximizar_restaurar_redimensionar", duracao_s=3.0, timeout_ms=25, acao_func=_acao_transicao_janela, win_hwnd=hwnd_explorer)
        relatorio["cenarios"]["6_maximizar_restaurar_redimensionar"] = r6
        time.sleep(0.5)

        # =====================================================================
        # CENÁRIO 7: Recuperação Automática de DXGI_ERROR_ACCESS_LOST
        # =====================================================================
        logger.info(">>> INICIANDO CENÁRIO: 7_recuperacao_access_lost")
        t_rec_inicio = time.time()
        
        # 1. Capturar frame normal
        f1 = dupl.acquire_frame(timeout_ms=50, capture_pixels=True)
        status_f1 = f1["status"]
        
        # 2. Forçar simulação de invalidação da duplicator COM
        dupl.force_access_lost_simulation()
        
        # 3. Próxima chamada deve detectar o erro e auto-recuperar
        t_rec_call = time.perf_counter()
        f_rec = dupl.acquire_frame(timeout_ms=50, capture_pixels=True)
        rec_lat_ms = (time.perf_counter() - t_rec_call) * 1000.0
        
        # 4. Próxima chamada deve adquirir frame normalmente no novo duplicator
        f_pos = dupl.acquire_frame(timeout_ms=50, capture_pixels=True)
        status_pos = f_pos["status"]
        
        r7 = {
            "cenario": "7_recuperacao_access_lost",
            "frame_antes_status": status_f1,
            "frame_recuperacao_status": f_rec["status"],
            "latencia_recuperacao_ms": round(rec_lat_ms, 2),
            "frame_pos_recuperacao_status": status_pos,
            "recuperacao_bem_sucedida": (status_pos == "ok" or f_rec["status"] == "recovered_after_access_lost")
        }
        relatorio["cenarios"]["7_recuperacao_access_lost"] = r7
        logger.info("  [CONCLUÍDO 7_recuperacao_access_lost]: Status pós: %s | Latência: %.2fms | Sucesso: %s",
                    status_pos, rec_lat_ms, r7["recuperacao_bem_sucedida"])

    finally:
        dupl.release()
        
    caminho_json = os.path.join(os.path.dirname(__file__), "relatorio_seguranca_idxgi.json")
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    print("\n" + "=" * 80)
    print(f"SEGUNDA BATERIA CONCLUÍDA! Relatório salvo em: {caminho_json}")
    print("=" * 80)
    return relatorio

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(rodar_bateria_seguranca())
        except Exception as e:
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=240)
    if err:
        raise err[0]
