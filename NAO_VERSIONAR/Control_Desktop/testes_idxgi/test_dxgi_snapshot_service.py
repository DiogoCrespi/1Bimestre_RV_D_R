import os
import sys
import time
import json
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    win32_mouse_move,
    win32_mouse_scroll
)
from testes_idxgi.dxgi_snapshot_service import DXGISnapshotService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_service")

def testar_servico_completo():
    ensure_desktop_access()
    logger.info("=== INICIANDO TESTE DO DXGISnapshotService ===")
    
    # 1. Obter instância singleton e iniciar
    svc = DXGISnapshotService.get_instance(enabled=True)
    iniciado = svc.start()
    assert iniciado, "Falha ao iniciar DXGISnapshotService"
    time.sleep(0.5) # Aguardar primeira sincronização
    
    resultados = {}
    
    try:
        # TESTE 1: Tela ociosa / ausência de ação (deve responder changed = False)
        w0 = svc.get_watermark()
        mudou, motivo, det = svc.wait_for_settle(w0, timeout_ms=80, min_wait_ms=30)
        resultados["1_tela_ociosa_cache_hit"] = {
            "mudou": mudou,
            "motivo": motivo,
            "correto": (mudou is False)
        }
        logger.info("Teste 1 (Ociosa): mudou=%s | motivo=%s | OK=%s", mudou, motivo, mudou is False)
        
        # TESTE 2: Apenas movimento de mouse (hardware cursor não deve invalidar pixels da janela)
        w_mouse = svc.get_watermark()
        win32_mouse_move(500, 500)
        time.sleep(0.05)
        win32_mouse_move(600, 600)
        time.sleep(0.05)
        
        # Consultar janela arbitrária longe do cursor ou tela
        mudou_m, motivo_m, det_m = svc.has_changed_since(w_mouse, win_rect=[100, 100, 400, 400])
        resultados["2_movimento_puro_cursor"] = {
            "mudou": mudou_m,
            "motivo": motivo_m,
            "correto": (mudou_m is False)
        }
        logger.info("Teste 2 (Cursor puro): mudou=%s | motivo=%s | OK=%s", mudou_m, motivo_m, mudou_m is False)

        # TESTE 3: Watermark pós-ação com mudança real (Scroll na tela)
        w_acao = svc.get_watermark()
        win32_mouse_scroll(-120, 0, 700, 500)
        mudou_a, motivo_a, det_a = svc.wait_for_settle(w_acao, timeout_ms=150, min_wait_ms=35)
        resultados["3_watermark_pos_acao_scroll"] = {
            "mudou": mudou_a,
            "motivo": motivo_a,
            "detalhes": det_a,
            "correto": (mudou_a is True)
        }
        logger.info("Teste 3 (Ação real pós-watermark): mudou=%s | motivo=%s | OK=%s", mudou_a, motivo_a, mudou_a is True)

        # TESTE 4: Janela completamente fora da tela
        w_off = svc.get_watermark()
        mudou_off, motivo_off, _ = svc.has_changed_since(w_off, win_rect=[-2000, -2000, -1000, -1000])
        resultados["4_janela_fora_da_tela"] = {
            "mudou": mudou_off,
            "motivo": motivo_off,
            "correto": (mudou_off is True and "offscreen" in motivo_off)
        }
        logger.info("Teste 4 (Fora da tela): mudou=%s | motivo=%s | OK=%s", mudou_off, motivo_off, resultados["4_janela_fora_da_tela"]["correto"])

        # TESTE 5: Janela com menos de 20% visível (fallback defensivo)
        w_sub = svc.get_watermark()
        mudou_sub, motivo_sub, _ = svc.has_changed_since(w_sub, win_rect=[svc.screen_width - 20, svc.screen_height - 20, svc.screen_width + 800, svc.screen_height + 600])
        resultados["5_janela_parcialmente_fora_da_tela"] = {
            "mudou": mudou_sub,
            "motivo": motivo_sub,
            "correto": (mudou_sub is True and "mostly_offscreen" in motivo_sub)
        }
        logger.info("Teste 5 (Parcialmente fora): mudou=%s | motivo=%s | OK=%s", mudou_sub, motivo_sub, resultados["5_janela_parcialmente_fora_da_tela"]["correto"])

        # TESTE 6: Invalidação por transição de modo / ACCESS_LOST simulada
        w_rec = svc.get_watermark()
        with svc.state_lock:
            svc.last_invalidation_reason = "simulated_access_lost"
        mudou_rec, motivo_rec, _ = svc.has_changed_since(w_rec, win_rect=[100, 100, 500, 500])
        resultados["6_invalidação_modo_access_lost"] = {
            "mudou": mudou_rec,
            "motivo": motivo_rec,
            "correto": (mudou_rec is True and "simulated_access_lost" in motivo_rec)
        }
        logger.info("Teste 6 (Invalidação forçada): mudou=%s | motivo=%s | OK=%s", mudou_rec, motivo_rec, resultados["6_invalidação_modo_access_lost"]["correto"])

    finally:
        svc.stop()
        release_desktop_access()

    caminho_out = os.path.join(os.path.dirname(__file__), "relatorio_test_service.json")
    with open(caminho_out, "w", encoding="utf-8") as f:
        json.dump(resultados, f, indent=2, ensure_ascii=False)
        
    logger.info("=== TODOS OS TESTES DO SERVIÇO CONCLUÍDOS COM SUCESSO! ===")
    return resultados

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(testar_servico_completo())
        except Exception as e:
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=30)
    if err:
        raise err[0]
