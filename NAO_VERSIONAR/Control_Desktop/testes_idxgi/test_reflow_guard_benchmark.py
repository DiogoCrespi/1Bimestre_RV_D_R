import os
import sys
import time
import json
import logging
from PIL import ImageGrab

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    _execute_system_action,
    focus_window,
    get_foreground_window_info,
    _ocr_candidates
)
from testes_idxgi.regional_ocr_shadow import (
    smart_regional_ocr_pipeline,
    compare_mark_sets
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reflow_benchmark")

def run_benchmark():
    ensure_desktop_access()
    logger.info("======================================================================")
    logger.info("INICIANDO BENCHMARK COM GUARDA ESTRUTURAL CONTRA REFLOW (UNITY 6.5)")
    logger.info("======================================================================")
    
    focus_window(title_kw="Unity")
    time.sleep(0.5)
    fw = get_foreground_window_info()
    assert fw and "unity" in fw.get("process", "").lower(), f"Unity não focada: {fw}"
    win_rect = [fw["left"], fw["top"], fw["left"] + fw["width"], fw["top"] + fw["height"]]
    logger.info("Janela Unity: HWND=%s Rect=%s", fw["hwnd"], win_rect)
    
    relatorio = {
        "unity_hwnd": fw["hwnd"],
        "unity_rect": win_rect,
        "scenarios": []
    }
    
    def testar_cenario(nome, desc, dirty_region, acao_fn):
        logger.info("\n--- CENÁRIO: %s (%s) ---", nome, desc)
        
        # 1. Captura pré-ação
        img_pre = ImageGrab.grab()
        marks_pre = _ocr_candidates(img_pre)
        logger.info("  Pré-Ação: %d marcas", len(marks_pre))
        
        # 2. Executa a ação real
        acao_fn()
        time.sleep(0.4)
        
        # 3. Captura pós-ação
        img_post = ImageGrab.grab()
        
        # 4. Ground Truth Oficial (Full OCR)
        t_f0 = time.time()
        marks_full_gt = _ocr_candidates(img_post)
        lat_full_gt = (time.time() - t_f0) * 1000.0
        
        # 5. Pipeline Inteligente (Regional + Guarda Estrutural + Fallback Automático)
        res_pipeline = smart_regional_ocr_pipeline(
            full_img=img_post,
            previous_marks=marks_pre,
            dirty_regions=dirty_region,
            win_rect=win_rect,
            max_area_pct=20.0
        )
        
        marks_result = res_pipeline["marks"]
        comp = compare_mark_sets(marks_result, marks_full_gt, iou_threshold=0.45)
        
        logger.info("  Resultado: Modo=%s | Motivo=%s | Latência=%.1fms (vs Full %.1fms) | Área=%.1f%%",
                    res_pipeline["mode"], res_pipeline["reason"], res_pipeline["latency_ms"], lat_full_gt, res_pipeline["area_pct"])
        logger.info("  Equivalência: %.1f%% | Perdidos: %d | Falsos/Obsoletos: %d",
                    comp["equivalencia_pct"], comp["perdidos_count"], comp["falsos_count"])
        
        item = {
            "cenario": nome,
            "descricao": desc,
            "pipeline_mode": res_pipeline["mode"],
            "pipeline_reason": res_pipeline["reason"],
            "is_fallback": res_pipeline["fallback"],
            "area_pct": res_pipeline["area_pct"],
            "pipeline_latency_ms": res_pipeline["latency_ms"],
            "full_ocr_latency_ms": round(lat_full_gt, 1),
            "speedup": round(lat_full_gt / max(0.1, res_pipeline["latency_ms"]), 2),
            "equivalencia_pct": comp["equivalencia_pct"],
            "elementos_perdidos": comp["perdidos_count"],
            "marcas_obsoletas_falsas": comp["falsos_count"],
            "zero_obsoletas_ok": comp["falsos_count"] == 0,
            "zero_perdidos_ok": comp["perdidos_count"] == 0
        }
        relatorio["scenarios"].append(item)
        time.sleep(0.3)
        return item

    # 1. Menu e Modal (Help Menu) - área pequena e isolada sem reflow
    def act_menu():
        _execute_system_action({"action": "click", "x": 611, "y": 77, "journal": False})
    testar_cenario("1_menu_popup_help", "Abertura de menu popup isolado", [[580, 70, 850, 420]], act_menu)
    _execute_system_action({"action": "key_combination", "keys": ["escape"], "journal": False})
    time.sleep(0.3)

    # 2. Foldout grande no Inspector - Container de reflow
    # A guarda estrutural deve expandir até a base do painel. Se exceder 20%, deve fazer fallback transparente para Full OCR!
    def act_foldout():
        _execute_system_action({"action": "click", "x": 1421, "y": 190, "journal": False})
    testar_cenario("2_foldout_inspector", "Foldout no Inspector com reflow de componentes inferiores", [[1380, 150, 1920, 350]], act_foldout)

    # 3. Scroll no Console - área de scroll
    def act_scroll():
        _execute_system_action({"action": "click", "x": 165, "y": 741, "journal": False})
        time.sleep(0.2)
        _execute_system_action({"action": "scroll", "dy": -120, "x": 300, "y": 850, "journal": False})
    testar_cenario("3_scroll_console", "Scroll de linhas de texto no Console", [[0, 700, 750, 1060]], act_scroll)
    _execute_system_action({"action": "click", "x": 57, "y": 742, "journal": False})
    time.sleep(0.3)

    # 4. Árvore / Lista na Hierarchy [0, 100, 350, 700]
    def act_hierarchy():
        _execute_system_action({"action": "click", "x": 70, "y": 200, "journal": False})
    testar_cenario("4_arvore_hierarchy", "Seleção de nó na árvore de Hierarchy", [[10, 180, 320, 240]], act_hierarchy)

    # 5. Dois painéis alterados simultaneamente (Hierarchy + Inspector)
    def act_dois_paineis():
        _execute_system_action({"action": "click", "x": 70, "y": 250, "journal": False})
    testar_cenario("5_dois_paineis_simultaneos", "Hierarchy selecionando nó com update completo no Inspector", [[10, 230, 320, 280], [1380, 150, 1920, 600]], act_dois_paineis)

    out_file = os.path.join(os.path.dirname(__file__), "relatorio_reflow_guard_benchmark.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    logger.info("======================================================================")
    logger.info("BENCHMARK CONCLUÍDO! Salvo em: %s", out_file)
    logger.info("======================================================================")
    return relatorio

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(run_benchmark())
        except Exception as e:
            logger.exception("Erro no benchmark")
            err.append(e)
        finally:
            release_desktop_access()
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=180)
    if err:
        raise err[0]
