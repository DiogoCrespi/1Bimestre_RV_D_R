import os
import sys
import time
import json
import logging
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    get_system_state,
    execute_system_action,
    get_dxgi_oracle,
    publish_state_snapshot
)
import remote_control_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_settle_races")

def testar_settle_races():
    ensure_desktop_access()
    logger.info("=== BATERIA DE VALIDAÇÃO: RESOLUÇÃO DE RACES NO SETTLE & WATERMARK ===")
    
    remote_control_server.ENABLE_DXGI_ORACLE = True
    oracle = get_dxgi_oracle()
    assert oracle is not None, "Falha ao instanciar Oráculo DXGI"
    time.sleep(0.4)
    oracle.paused = True
    
    # Coordenadas da Janela-Alvo (Unity) e Janela Externa (Chrome)
    unity_rect = [0, 0, 960, 1080]       # Metade esquerda
    outra_janela_rect = [960, 0, 1920, 1080] # Metade direita
    
    relatorio = {}
    
    # =========================================================================
    # TESTE 1: Unity parada + Animação contínua rodando em outra janela
    # =========================================================================
    logger.info("--- TESTE 1: Alvo estático + Animação concorrente em outra janela ---")
    
    # Simular animação contínua em outra janela injetando dirty rects fora do alvo
    animacao_ativa = [True]
    def _gerador_animacao_externa():
        while animacao_ativa[0]:
            with oracle.state_lock:
                oracle.generation += 1
                oracle.last_present_time = time.time()
                # Retângulo dirty estritamente na outra janela (x >= 1200)
                oracle.history[oracle.generation] = {
                    "union_rects": [[1200, 200, 1400, 400]],
                    "timestamp": time.time(),
                    "accumulated_frames": 1
                }
            time.sleep(0.016) # 60 FPS
            
    # 1.1 Baseline com alvo inalterado
    w_base = oracle.get_watermark()
    t_anim = threading.Thread(target=_gerador_animacao_externa, daemon=True)
    t_anim.start()
    
    try:
        time.sleep(0.08) # frames externos chegam
        
        # Durante ociosidade, /state no alvo deve dar CACHE HIT mesmo com a outra janela animando!
        mudou, motivo, det = oracle.has_changed_since(w_base, win_rect=unity_rect)
        assert mudou is False, f"Alvo deveria estar inalterado, mas acusou mudou={mudou}"
        assert motivo == "changes_outside_target_window"
        logger.info("1.1 Cache Hit no alvo preservado durante animação externa: motivo=%s (OK)", motivo)
        
        # 1.2 Ação disparada no alvo enquanto a outra janela anima:
        # Settle NÃO deve encerrar prematuramente ao ver os frames da outra janela!
        t_settle_start = time.time()
        w_acao = oracle.get_watermark()
        # Settle monitorando especificamente unity_rect com timeout de 100ms
        mudou_s, motivo_s, det_s = oracle.wait_for_settle(w_acao, win_rect=unity_rect, timeout_ms=100, min_wait_ms=30)
        waited_ms = (time.time() - t_settle_start) * 1000.0
        
        # Como o alvo não emitiu frames, o settle deve esperar o timeout completo (>= 100ms)
        # e aplicar a postura conservadora (action_target_unsettled_other_windows_active),
        # em vez de sair aos 30ms com falso cache hit!
        assert waited_ms >= 90.0, f"Settle encerrou prematuramente aos {waited_ms:.1f}ms devido à outra janela!"
        assert mudou_s is True, "Incerteza pós-ação deveria invalidar o cache"
        assert "other_windows_active" in motivo_s
        logger.info("1.2 Settle esperou timeout (%.1fms) sem falso encerramento prematuro: motivo=%s (OK)", waited_ms, motivo_s)
        
        relatorio["1_alvo_estatico_animacao_externa"] = {
            "cache_hit_em_ociosidade_ok": True,
            "motivo_cache_hit": motivo,
            "settle_esperou_timeout_ms": round(waited_ms, 1),
            "motivo_invalidação_defensiva": motivo_s,
            "falso_cache_hit_evitado": True
        }
    finally:
        animacao_ativa[0] = False
        t_anim.join(timeout=0.5)

    # =========================================================================
    # TESTE 2: Efeito com atraso de 50, 100, 250 e 500 ms
    # =========================================================================
    logger.info("--- TESTE 2: Ação com atrasos de resposta do alvo (50, 100, 250, 500 ms) ---")
    relatorio["2_atrasos_resposta_alvo"] = {}
    
    for delay_ms in [50, 100, 250, 500]:
        w_pre = oracle.get_watermark()
        
        # Thread que simula a apresentação do frame pelo alvo após o atraso estipulado
        def _injetar_frame_com_atraso(d_ms):
            time.sleep(d_ms / 1000.0)
            with oracle.state_lock:
                oracle.generation += 1
                oracle.last_present_time = time.time()
                # Dirty rect no alvo (Unity)
                oracle.history[oracle.generation] = {
                    "union_rects": [[200, 200, 400, 400]],
                    "timestamp": time.time(),
                    "accumulated_frames": 1
                }
        th = threading.Thread(target=_injetar_frame_com_atraso, args=(delay_ms,), daemon=True)
        th.start()
        
        # Settle configurado com timeout padrão de 150 ms
        t0 = time.time()
        mudou_d, motivo_d, det_d = oracle.wait_for_settle(w_pre, win_rect=unity_rect, timeout_ms=150, min_wait_ms=25)
        elapsed_ms = (time.time() - t0) * 1000.0
        th.join(timeout=1.0)
        
        logger.info("  Atraso %dms: mudou=%s | motivo='%s' | tempo=%.1fms", delay_ms, mudou_d, motivo_d, elapsed_ms)
        assert mudou_d is True, f"FALHA GRAVE: Atraso de {delay_ms}ms produziu falso cache hit!"
        
        if delay_ms <= 100:
            assert motivo_d in ("target_window_changed", "target_settled_quiescent", "target_continuous_activity_timeout")
        else:
            assert ("action_target_unsettled" in motivo_d or "action_target_no_activity" in motivo_d or "continuous_activity" in motivo_d or motivo_d in ("target_window_changed", "target_settled_quiescent"))
            
        relatorio["2_atrasos_resposta_alvo"][f"{delay_ms}ms"] = {
            "mudou": mudou_d,
            "motivo": motivo_d,
            "tempo_settle_ms": round(elapsed_ms, 1),
            "falso_cache_hit_evitado": True
        }

    # =========================================================================
    # TESTE 3: Ação que não produz nenhuma mudança visual
    # =========================================================================
    logger.info("--- TESTE 3: Ação que não produz nenhuma alteração visual ---")
    w_no_vis = oracle.get_watermark()
    # Nenhuma alteração visual injetada
    mudou_nv, motivo_nv, _ = oracle.wait_for_settle(w_no_vis, win_rect=unity_rect, timeout_ms=80, min_wait_ms=25)
    # Postura conservadora: ausência de reação após ação invalida o cache para segurança
    logger.info("3. Ação inerte: mudou=%s | motivo='%s'", mudou_nv, motivo_nv)
    assert mudou_nv is True
    assert ("action_target" in motivo_nv or motivo_nv == "target_window_changed")
    relatorio["3_acao_sem_mudanca_visual"] = {
        "mudou": mudou_nv,
        "motivo": motivo_nv,
        "seguranca_conservadora_ok": True
    }

    # =========================================================================
    # TESTE 4: Ação que altera outra região da mesma janela
    # =========================================================================
    logger.info("--- TESTE 4: Ação que altera outra região da mesma janela ---")
    w_same = oracle.get_watermark()
    # Clique em (100, 100), mas alteração visual ocorre no rodapé da mesma janela (100, 900)
    with oracle.state_lock:
        oracle.generation += 1
        oracle.history[oracle.generation] = {
            "union_rects": [[50, 850, 300, 950]], # Rodapé dentro do unity_rect
            "timestamp": time.time(),
            "accumulated_frames": 1
        }
    mudou_sw, motivo_sw, _ = oracle.wait_for_settle(w_same, win_rect=unity_rect, timeout_ms=80, min_wait_ms=10)
    assert mudou_sw is True
    assert motivo_sw in ("target_window_changed", "target_settled_quiescent")
    logger.info("4. Alteração em outra região da janela capturada: motivo='%s' (OK)", motivo_sw)
    relatorio["4_alteracao_outra_regiao_mesma_janela"] = {
        "mudou": mudou_sw,
        "motivo": motivo_sw,
        "captura_integral_ok": True
    }

    # =========================================================================
    # TESTE 5: Alteração espontânea da janela-alvo sem ação do agente
    # =========================================================================
    logger.info("--- TESTE 5: Alteração espontânea do alvo durante observação ociosa ---")
    w_spont = oracle.get_watermark()
    # Unity emite frame espontâneo (ex: log no console)
    with oracle.state_lock:
        oracle.generation += 1
        oracle.history[oracle.generation] = {
            "union_rects": [[100, 400, 600, 450]],
            "timestamp": time.time(),
            "accumulated_frames": 1
        }
    mudou_sp, motivo_sp, det_sp = oracle.has_changed_since(w_spont, win_rect=unity_rect)
    assert mudou_sp is True
    assert ("target_window_dirty" in motivo_sp or "invalidated" in motivo_sp)
    logger.info("5. Alteração espontânea detectada em observação: motivo='%s' (OK)", motivo_sp)
    relatorio["5_alteracao_espontanea_sem_acao"] = {
        "mudou": mudou_sp,
        "motivo": motivo_sp,
        "detectado_ok": True
    }

    # =========================================================================
    # TESTE 6: Mudança ocorrendo imediatamente após o settle retornar
    # =========================================================================
    logger.info("--- TESTE 6: Mudança ocorrendo imediatamente após o retorno do settle ---")
    w_post = oracle.get_watermark()
    # Settle retorna (sem ver mudança até o momento)
    oracle.wait_for_settle(w_post, win_rect=unity_rect, timeout_ms=50, min_wait_ms=10)
    
    # 5ms depois, o frame atrasado finalmente chega
    time.sleep(0.005)
    with oracle.state_lock:
        oracle.generation += 1
        oracle.history[oracle.generation] = {
            "union_rects": [[300, 300, 500, 500]],
            "timestamp": time.time(),
            "accumulated_frames": 1
        }
        
    # Quando o próximo /state consulta has_changed_since com base no snapshot anterior:
    mudou_lat, motivo_lat, _ = oracle.has_changed_since(w_post, win_rect=unity_rect)
    assert mudou_lat is True
    assert "target" in motivo_lat or "unsettled" in motivo_lat
    logger.info("6. Mudança pós-settle interceptada com sucesso: motivo='%s' (OK)", motivo_lat)
    relatorio["6_mudanca_imediatamente_pos_settle"] = {
        "mudou": mudou_lat,
        "motivo": motivo_lat,
        "seguranca_ok": True
    }

    # Salvar relatório consolidado
    caminho_out = os.path.join(os.path.dirname(__file__), "relatorio_settle_races.json")
    with open(caminho_out, "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    oracle.paused = False
    logger.info("=== BATERIA COMPLETA DE RACES PASSOU COM 100%% DE SUCESSO! ===")
    return relatorio

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(testar_settle_races())
        except Exception as e:
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=30)
    if err:
        raise err[0]
