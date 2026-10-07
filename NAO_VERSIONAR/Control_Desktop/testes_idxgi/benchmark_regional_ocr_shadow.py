import os
import sys
import time
import json
import math
import psutil
import logging
from PIL import Image, ImageGrab

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    get_system_state,
    _execute_system_action,
    focus_window,
    get_foreground_window_info,
    _ocr_candidates
)
from testes_idxgi.regional_ocr_shadow import (
    merge_rectangles,
    expand_with_intersecting_marks,
    execute_regional_ocr_crops,
    reconcile_state_marks,
    compare_mark_sets
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark_regional_ocr")

def run_regional_ocr_benchmark():
    ensure_desktop_access()
    logger.info("======================================================================")
    logger.info("INICIANDO BENCHMARK SHADOW MODE: REGIONAL OCR vs FULL OCR (UNITY 6.5)")
    logger.info("======================================================================")
    
    # Foca na Unity
    focus_window(title_kw="Unity")
    time.sleep(0.5)
    fw = get_foreground_window_info()
    assert fw and "unity" in fw.get("process", "").lower(), f"Unity não focada: {fw}"
    win_rect = [fw["left"], fw["top"], fw["left"] + fw["width"], fw["top"] + fw["height"]]
    win_area = max(1, fw["width"] * fw["height"])
    logger.info("Janela Unity: HWND=%s Rect=%s Area=%d px", fw["hwnd"], win_rect, win_area)
    
    process = psutil.Process()
    relatorio = {
        "unity_hwnd": fw["hwnd"],
        "unity_rect": win_rect,
        "unity_area": win_area,
        "area_sweep": [],
        "real_scenarios": []
    }
    
    # -------------------------------------------------------------------------
    # PARTE 1: SWEEP DE ÁREA ALTERADA (5%, 10%, 15%, 25%, 50%)
    # -------------------------------------------------------------------------
    logger.info("\n--- PARTE 1: SWEEP DE ÁREA ALTERADA (5%, 10%, 15%, 25%, 50%) ---")
    # Captura tela base
    full_img = ImageGrab.grab()
    t_f0 = time.time()
    full_marks = _ocr_candidates(full_img)
    full_ocr_lat_ms = (time.time() - t_f0) * 1000.0
    logger.info("Full OCR Baseline: %d tags em %.1fms", len(full_marks), full_ocr_lat_ms)
    
    thresholds = [0.05, 0.10, 0.15, 0.25, 0.50]
    
    for th in thresholds:
        target_sub_area = win_area * th
        side_w = int(math.sqrt(target_sub_area * (fw["width"] / fw["height"])))
        side_h = int(target_sub_area / max(1, side_w))
        
        # Região central simulando mudança daquela proporção
        cx = fw["left"] + fw["width"] // 2
        cy = fw["top"] + fw["height"] // 2
        r_l = max(fw["left"], cx - side_w // 2)
        r_t = max(fw["top"], cy - side_h // 2)
        r_r = min(win_rect[2], r_l + side_w)
        r_b = min(win_rect[3], r_t + side_h)
        sim_region = [[r_l, r_t, r_r, r_b]]
        
        # Expansão e OCR regional
        cpu_pre = process.cpu_percent(interval=None)
        t_reg0 = time.time()
        exp_regions = expand_with_intersecting_marks(sim_region, full_marks, win_rect=win_rect)
        reg_marks, reg_crop_lat = execute_regional_ocr_crops(full_img, exp_regions, win_left=0, win_top=0)
        reconciled_marks, recon_lat = reconcile_state_marks(full_marks, reg_marks, exp_regions)
        t_reg1 = time.time()
        total_reg_lat_ms = (t_reg1 - t_reg0) * 1000.0
        cpu_post = process.cpu_percent(interval=None)
        
        comp = compare_mark_sets(reconciled_marks, full_marks, iou_threshold=0.45)
        reprocessed_area = sum((r[2]-r[0])*(r[3]-r[1]) for r in exp_regions)
        reprocessed_pct = round((reprocessed_area / win_area) * 100.0, 1)
        
        logger.info("Sweep Area %.0f%% (real reproc: %.1f%%): Regional=%.1fms vs Full=%.1fms | Equivalência=%.1f%%",
                    th * 100, reprocessed_pct, total_reg_lat_ms, full_ocr_lat_ms, comp["equivalencia_pct"])
        
        relatorio["area_sweep"].append({
            "target_pct": int(th * 100),
            "reprocessed_pct": reprocessed_pct,
            "regional_total_latency_ms": round(total_reg_lat_ms, 1),
            "regional_crop_ocr_ms": round(reg_crop_lat, 1),
            "reconciliation_ms": round(recon_lat, 2),
            "full_ocr_latency_ms": round(full_ocr_lat_ms, 1),
            "speedup_factor": round(full_ocr_lat_ms / max(0.1, total_reg_lat_ms), 2),
            "equivalencia_pct": comp["equivalencia_pct"],
            "perdidos": comp["perdidos_count"],
            "falsos_duplicados": comp["falsos_count"],
            "is_faster_than_full": total_reg_lat_ms < full_ocr_lat_ms
        })

    # -------------------------------------------------------------------------
    # PARTE 2: BATERIA EM CENÁRIOS REAIS NA UNITY
    # -------------------------------------------------------------------------
    logger.info("\n--- PARTE 2: CENÁRIOS REAIS NA UNITY COM COMPARAÇÃO SHADOW ---")
    
    def testar_acao_shadow(nome, desc, dirty_rect_estimado, acao_fn):
        logger.info("Testando Cenário: %s (%s)...", nome, desc)
        
        # 1. Estado prévio
        img_pre = ImageGrab.grab()
        marks_pre = _ocr_candidates(img_pre)
        
        # 2. Executa a ação real
        acao_fn()
        time.sleep(0.3) # aguarda pintura
        
        # 3. Estado pós
        img_post = ImageGrab.grab()
        
        # 4. Full OCR de referência (Ground Truth)
        t_f0 = time.time()
        marks_full_gt = _ocr_candidates(img_post)
        lat_full_gt = (time.time() - t_f0) * 1000.0
        
        # 5. Execução Regional em Shadow
        t_r0 = time.time()
        exp_regions = expand_with_intersecting_marks(dirty_rect_estimado, marks_pre, win_rect=win_rect)
        marks_reg, lat_crop_reg = execute_regional_ocr_crops(img_post, exp_regions)
        reconciled_marks, lat_recon = reconcile_state_marks(marks_pre, marks_reg, exp_regions)
        lat_reg_total = (time.time() - t_r0) * 1000.0
        
        # 6. Comparação estrita
        comp = compare_mark_sets(reconciled_marks, marks_full_gt, iou_threshold=0.45)
        reproc_area = sum((r[2]-r[0])*(r[3]-r[1]) for r in exp_regions)
        reproc_pct = round((reproc_area / win_area) * 100.0, 1)
        
        logger.info("  -> Regional: %.1fms | Full GT: %.1fms | Equivalência: %.1f%% | Perdidos: %d | Falsos: %d",
                    lat_reg_total, lat_full_gt, comp["equivalencia_pct"], comp["perdidos_count"], comp["falsos_count"])
        
        res = {
            "cenario": nome,
            "descricao": desc,
            "reprocessed_area_pct": reproc_pct,
            "regional_latency_ms": round(lat_reg_total, 1),
            "full_ocr_latency_ms": round(lat_full_gt, 1),
            "speedup": round(lat_full_gt / max(0.1, lat_reg_total), 2),
            "equivalencia_pct": comp["equivalencia_pct"],
            "total_full_tags": comp["total_full"],
            "total_reconciled_tags": comp["total_reconciled"],
            "elementos_perdidos": comp["perdidos_count"],
            "falsos_duplicados": comp["falsos_count"],
            "perdidos_sample": comp["perdidos_sample"],
            "falsos_sample": comp["falsos_sample"]
        }
        relatorio["real_scenarios"].append(res)
        time.sleep(0.3)
        return res

    # Cenário A: Menu e Modal (Menu Help)
    # Dirty region: barra superior Help + área do popup [580, 70, 850, 400]
    def act_menu_help():
        _execute_system_action({"action": "click", "x": 611, "y": 77, "journal": False})
    testar_acao_shadow(
        "menu_popup_help",
        "Abertura de menu popup Help",
        [[580, 70, 850, 420]],
        act_menu_help
    )
    # Fecha o menu
    _execute_system_action({"action": "key_combination", "keys": ["escape"], "journal": False})
    time.sleep(0.4)
    
    # Cenário B: Foldout no Inspector [1400, 180, 1900, 500]
    def act_foldout():
        _execute_system_action({"action": "click", "x": 1421, "y": 190, "journal": False})
    testar_acao_shadow(
        "foldout_inspector",
        "Alternar componente no Inspector",
        [[1380, 150, 1920, 550]],
        act_foldout
    )
    
    # Cenário C: Scroll no Console [0, 700, 700, 1050]
    def act_scroll():
        # Clica na aba Console e rola
        _execute_system_action({"action": "click", "x": 165, "y": 741, "journal": False})
        time.sleep(0.2)
        _execute_system_action({"action": "scroll", "dy": -120, "x": 300, "y": 850, "journal": False})
    testar_acao_shadow(
        "scroll_console",
        "Troca de aba Console e Scroll",
        [[0, 700, 750, 1060]],
        act_scroll
    )
    
    # Retorna para aba Project
    _execute_system_action({"action": "click", "x": 57, "y": 742, "journal": False})
    time.sleep(0.4)
    
    # Cenário D: Duas regiões mudando simultaneamente (Inspector + Project)
    def act_duas_regioes():
        # Clica em um item do Project para mudar a seleção e atualizar o Inspector ao mesmo tempo
        _execute_system_action({"action": "click", "x": 150, "y": 800, "journal": False})
    testar_acao_shadow(
        "duas_regioes_simultaneas",
        "Seleção no Project disparando update no Inspector",
        [[50, 750, 400, 950], [1380, 150, 1920, 800]],
        act_duas_regioes
    )
    
    # Salva relatório
    out_file = os.path.join(os.path.dirname(__file__), "relatorio_regional_ocr_shadow.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    logger.info("======================================================================")
    logger.info("BENCHMARK FINALIZADO! Relatório gravado em: %s", out_file)
    logger.info("======================================================================")
    return relatorio

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(run_regional_ocr_benchmark())
        except Exception as e:
            logger.exception("Erro no benchmark regional")
            err.append(e)
        finally:
            release_desktop_access()
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=180)
    if err:
        raise err[0]
