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
    get_dxgi_oracle,
)
import remote_control_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_quiescence")

def benchmark_quiescencia():
    ensure_desktop_access()
    logger.info("================================================================================")
    logger.info("   BENCHMARK E VALIDAÇÃO DE QUIESCÊNCIA PÓS-AÇÃO NO ORÁCULO DXGI")
    logger.info("================================================================================")
    
    remote_control_server.ENABLE_DXGI_ORACLE = True
    oracle = get_dxgi_oracle()
    assert oracle is not None, "Falha ao obter instância do DXGISnapshotService"
    time.sleep(0.3)
    
    try:
        # Pausa a captura física do desktop para isolar estritamente os eventos do benchmark sintético
        oracle.paused = True
        time.sleep(0.05)
        
        # Geometria simulada da tela e janelas:
        # Monitor: 1920x1080
        # Janela-alvo (Unity): [0, 0, 960, 1080]
        # Janela externa (Chrome/Vídeo): [960, 0, 1920, 1080]
        unity_rect = [0, 0, 960, 1080]
        outra_janela_rect = [960, 0, 1920, 1080]
        
        relatorio = {
            "cenarios": {},
            "metricas_consolidadas": {
                "total_cenarios": 0,
                "falsos_cache_hits": 0,
                "snapshots_prematuros": 0,
                "casos_nunca_quiescentes": 0,
                "tempos_estabilizacao_ms": {}
            }
        }
        
        # -------------------------------------------------------------------------
        # Helper: Injeta frame sintético no histórico do oráculo
        # -------------------------------------------------------------------------
        def _injetar_frame(rects):
            with oracle.state_lock:
                oracle.generation += 1
                oracle.last_present_time = time.time()
                oracle.history[oracle.generation] = {
                    "union_rects": rects,
                    "timestamp": time.time(),
                    "accumulated_frames": 1
                }
                
        # =========================================================================
        # CENÁRIO 1: Reação após atraso de 50ms, 100ms, 250ms e 500ms
        # =========================================================================
        logger.info("\n--- CENÁRIO 1: Reação após atrasos calibrados (50, 100, 250, 500 ms) ---")
        delays_test = [50, 100, 250, 500]
        c1_results = {}
        
        for d_ms in delays_test:
            wm = oracle.get_watermark()
            t_start = time.time()
            
            # Agenda um dirty rect único exatamente no tempo d_ms
            def _job_atrasado(delay_target):
                time.sleep(delay_target / 1000.0)
                _injetar_frame([[200, 200, 400, 400]]) # dentro da Unity
                
            th = threading.Thread(target=_job_atrasado, args=(d_ms,), daemon=True)
            th.start()
            
            max_to = max(350, d_ms + 150)
            mudou, motivo, det = oracle.wait_for_settle(
                wm, win_rect=unity_rect,
                quiescence_window_ms=40,
                min_observe_ms=20,
                max_timeout_ms=max_to
            )
            elapsed_ms = det.get("elapsed_ms", (time.time() - t_start) * 1000.0)
            th.join(timeout=1.0)
            
            # Verificações fundamentais:
            # 1. mudou DEVE ser True (houve alteração)
            # 2. O settle NÃO pode encerrar antes do atraso (Snapshot Prematuro: elapsed_ms < d_ms)
            # 3. O settle DEVE esperar o período de quiescência: elapsed_ms >= d_ms + 35ms
            snapshot_prematuro = elapsed_ms < d_ms
            falso_cache_hit = not mudou
            
            logger.info("  Atraso %3dms -> Settle: %6.1fms | motivo: %-30s | Quiescent: %s | Prematuro: %s",
                        d_ms, elapsed_ms, motivo, det.get("quiescent"), snapshot_prematuro)
                        
            assert not falso_cache_hit, f"Falso cache hit no atraso {d_ms}ms"
            assert not snapshot_prematuro, f"Snapshot prematuro! Settle retornou em {elapsed_ms}ms antes do frame em {d_ms}ms"
            assert det.get("quiescent") is True, f"Esperava quiescente, obteve motivo={motivo}"
            
            c1_results[f"delay_{d_ms}ms"] = {
                "delay_configurado_ms": d_ms,
                "tempo_estabilizacao_ms": elapsed_ms,
                "motivo": motivo,
                "quiescent": det.get("quiescent"),
                "snapshot_prematuro": snapshot_prematuro,
                "falso_cache_hit": falso_cache_hit
            }
            relatorio["metricas_consolidadas"]["tempos_estabilizacao_ms"][f"reacao_{d_ms}ms"] = elapsed_ms
            if falso_cache_hit:
                relatorio["metricas_consolidadas"]["falsos_cache_hits"] += 1
            if snapshot_prematuro:
                relatorio["metricas_consolidadas"]["snapshots_prematuros"] += 1
                
        relatorio["cenarios"]["1_reacao_com_atrasos"] = c1_results
        
        # =========================================================================
        # CENÁRIO 2: Caret piscando / Animação contínua na mesma janela
        # =========================================================================
        logger.info("\n--- CENÁRIO 2: Caret piscando / Animação contínua na mesma janela ---")
        caret_ativo = [True]
        def _caret_loop():
            while caret_ativo[0]:
                _injetar_frame([[500, 300, 502, 320]]) # Linha vertical do cursor na Unity
                time.sleep(0.015)
                
        t_caret = threading.Thread(target=_caret_loop, daemon=True)
        t_caret.start()
        
        try:
            wm2 = oracle.get_watermark()
            t0 = time.time()
            mudou, motivo, det = oracle.wait_for_settle(
                wm2, win_rect=unity_rect,
                quiescence_window_ms=40,
                min_observe_ms=20,
                max_timeout_ms=180
            )
            elapsed_ms = det.get("elapsed_ms", (time.time() - t0) * 1000.0)
            
            falso_cache_hit = not mudou
            never_quiescent = det.get("never_quiescent", False)
            
            logger.info("  Caret contínuo -> Settle: %.1fms | motivo: %s | never_quiescent: %s",
                        elapsed_ms, motivo, never_quiescent)
                        
            assert not falso_cache_hit, "Caret ativo causou falso cache hit!"
            assert never_quiescent is True, f"Esperava never_quiescent=True devido à animação perpétua, obteve motivo={motivo}"
            assert motivo == "target_continuous_activity_timeout"
            assert det.get("fallback") is True
            
            relatorio["cenarios"]["2_caret_animacao_continua"] = {
                "elapsed_ms": elapsed_ms,
                "motivo": motivo,
                "never_quiescent": never_quiescent,
                "fallback_full_snapshot": True,
                "falso_cache_hit": falso_cache_hit
            }
            relatorio["metricas_consolidadas"]["casos_nunca_quiescentes"] += 1
        finally:
            caret_ativo[0] = False
            t_caret.join(timeout=0.5)

        # =========================================================================
        # CENÁRIO 3: Unity renderizando continuamente a 60 FPS
        # =========================================================================
        logger.info("\n--- CENÁRIO 3: Unity renderizando continuamente a 60 FPS (Viewport Ativa) ---")
        unity_60fps_ativo = [True]
        def _unity_render_loop():
            while unity_60fps_ativo[0]:
                _injetar_frame([[50, 50, 800, 600]])
                time.sleep(0.0166)
                
        t_unity = threading.Thread(target=_unity_render_loop, daemon=True)
        t_unity.start()
        
        try:
            wm3 = oracle.get_watermark()
            t0 = time.time()
            mudou, motivo, det = oracle.wait_for_settle(
                wm3, win_rect=unity_rect,
                quiescence_window_ms=40,
                min_observe_ms=20,
                max_timeout_ms=200
            )
            elapsed_ms = det.get("elapsed_ms", (time.time() - t0) * 1000.0)
            
            falso_cache_hit = not mudou
            never_quiescent = det.get("never_quiescent", False)
            
            logger.info("  Unity 60 FPS -> Settle: %.1fms | motivo: %s | never_quiescent: %s",
                        elapsed_ms, motivo, never_quiescent)
                        
            assert not falso_cache_hit, "Unity 60 FPS causou falso cache hit!"
            assert never_quiescent is True, f"Esperava never_quiescent=True para 60 FPS ininterruptos, obteve: {motivo}"
            assert motivo == "target_continuous_activity_timeout"
            
            relatorio["cenarios"]["3_unity_60fps_continuo"] = {
                "elapsed_ms": elapsed_ms,
                "motivo": motivo,
                "never_quiescent": never_quiescent,
                "fallback_full_snapshot": True,
                "falso_cache_hit": falso_cache_hit
            }
            relatorio["metricas_consolidadas"]["casos_nunca_quiescentes"] += 1
        finally:
            unity_60fps_ativo[0] = False
            t_unity.join(timeout=0.5)

        # =========================================================================
        # CENÁRIO 4: Botão que muda imediatamente (15ms) e conclui animação (110ms)
        # =========================================================================
        logger.info("\n--- CENÁRIO 4: Botão: efeito imediato (15ms) + finalização de animação (110ms) ---")
        wm4 = oracle.get_watermark()
        t0 = time.time()
        
        def _sequencia_botao():
            # Frame 1: efeito imediato de clique / highlight (15ms)
            time.sleep(0.015)
            _injetar_frame([[100, 100, 200, 140]])
            # Frame 2: animação intermediária aos 40ms (delta = 25ms < 40ms de quiescência)
            time.sleep(0.025)
            _injetar_frame([[100, 100, 200, 140]])
            # Frame 3: animação intermediária aos 65ms (delta = 25ms < 40ms)
            time.sleep(0.025)
            _injetar_frame([[100, 100, 200, 140]])
            # Frame 4: término da transição / animação aos 95ms (delta = 30ms < 40ms)
            time.sleep(0.030)
            _injetar_frame([[100, 100, 200, 140]])
            
        th_btn = threading.Thread(target=_sequencia_botao, daemon=True)
        th_btn.start()
        
        mudou, motivo, det = oracle.wait_for_settle(
            wm4, win_rect=unity_rect,
            quiescence_window_ms=40,
            min_observe_ms=20,
            max_timeout_ms=300
        )
        elapsed_ms = det.get("elapsed_ms", (time.time() - t0) * 1000.0)
        th_btn.join(timeout=1.0)
        
        snapshot_prematuro = elapsed_ms < 130.0
        falso_cache_hit = not mudou
        quiescent = det.get("quiescent", False)
        
        logger.info("  Botão composto -> Settle: %.1fms | eventos: %d | motivo: %s | Prematuro: %s",
                    elapsed_ms, det.get("activity_events", 0), motivo, snapshot_prematuro)
                    
        assert not falso_cache_hit, "Botão composto causou falso cache hit"
        assert not snapshot_prematuro, f"Snapshot prematuro! Settle retornou aos {elapsed_ms}ms antes do fim da animação"
        assert quiescent is True, f"Esperava estabilização quiescente, obteve motivo={motivo}"
        assert det.get("activity_events", 0) >= 3, "Deveria ter registrado todos os 3 eventos da sequência"
        
        relatorio["cenarios"]["4_botao_animacao_composta"] = {
            "elapsed_ms": elapsed_ms,
            "activity_events": det.get("activity_events"),
            "motivo": motivo,
            "quiescent": quiescent,
            "snapshot_prematuro": snapshot_prematuro,
            "falso_cache_hit": falso_cache_hit
        }
        relatorio["metricas_consolidadas"]["tempos_estabilizacao_ms"]["botao_animacao"] = elapsed_ms

        # =========================================================================
        # CENÁRIO 5: Ação sem efeito visual (inerte)
        # =========================================================================
        logger.info("\n--- CENÁRIO 5: Ação sem efeito visual (inerte / clique no vazio) ---")
        wm5 = oracle.get_watermark()
        t0 = time.time()
        
        mudou, motivo, det = oracle.wait_for_settle(
            wm5, win_rect=unity_rect,
            quiescence_window_ms=40,
            min_observe_ms=20,
            max_timeout_ms=120
        )
        elapsed_ms = det.get("elapsed_ms", (time.time() - t0) * 1000.0)
        
        falso_cache_hit = not mudou
        logger.info("  Ação inerte -> Settle: %.1fms | motivo: %s | fallback_full_snapshot: %s",
                    elapsed_ms, motivo, mudou)
                    
        assert not falso_cache_hit, "Ação inerte gerou falso cache hit perigoso!"
        assert motivo == "action_target_no_activity_timeout"
        assert det.get("fallback") is True
        assert det.get("target_activity_seen") is False
        
        relatorio["cenarios"]["5_acao_sem_efeito_visual"] = {
            "elapsed_ms": elapsed_ms,
            "motivo": motivo,
            "fallback_full_snapshot": True,
            "target_activity_seen": False,
            "falso_cache_hit": falso_cache_hit
        }
        relatorio["metricas_consolidadas"]["tempos_estabilizacao_ms"]["acao_inerte_timeout"] = elapsed_ms

        # =========================================================================
        # CENÁRIO 6: Ação que abre modal após atraso (180ms)
        # =========================================================================
        logger.info("\n--- CENÁRIO 6: Ação que abre modal após atraso (180ms) ---")
        wm6 = oracle.get_watermark()
        t0 = time.time()
        
        def _abertura_modal():
            time.sleep(0.180)
            _injetar_frame([[250, 200, 750, 650]])
            
        th_modal = threading.Thread(target=_abertura_modal, daemon=True)
        th_modal.start()
        
        mudou, motivo, det = oracle.wait_for_settle(
            wm6, win_rect=unity_rect,
            quiescence_window_ms=40,
            min_observe_ms=20,
            max_timeout_ms=350
        )
        elapsed_ms = det.get("elapsed_ms", (time.time() - t0) * 1000.0)
        th_modal.join(timeout=1.0)
        
        snapshot_prematuro = elapsed_ms < 180.0
        falso_cache_hit = not mudou
        quiescent = det.get("quiescent", False)
        
        logger.info("  Abertura modal (180ms) -> Settle: %.1fms | motivo: %s | Prematuro: %s",
                    elapsed_ms, motivo, snapshot_prematuro)
                    
        assert not falso_cache_hit, "Abertura de modal causou falso cache hit!"
        assert not snapshot_prematuro, f"Snapshot prematuro! Settle retornou aos {elapsed_ms}ms antes do modal aos 180ms"
        assert quiescent is True, f"Esperava estabilização quiescente após o modal, obteve {motivo}"
        
        relatorio["cenarios"]["6_abertura_modal_atraso"] = {
            "atraso_esperado_ms": 180,
            "elapsed_ms": elapsed_ms,
            "motivo": motivo,
            "quiescent": quiescent,
            "snapshot_prematuro": snapshot_prematuro,
            "falso_cache_hit": falso_cache_hit
        }
        relatorio["metricas_consolidadas"]["tempos_estabilizacao_ms"]["abertura_modal"] = elapsed_ms
        
        # Finalização e métricas agregadas
        relatorio["metricas_consolidadas"]["total_cenarios"] = len(delays_test) + 5
        
        caminho_rel = os.path.join(os.path.dirname(__file__), "relatorio_quiescencia.json")
        with open(caminho_rel, "w", encoding="utf-8") as f:
            json.dump(relatorio, f, indent=2, ensure_ascii=False)
            
        logger.info("\n================================================================================")
        logger.info("   BENCHMARK DE QUIESCÊNCIA CONCLUÍDO COM 100%% DE SUCESSO!")
        logger.info("   - Falsos Cache Hits:       %d", relatorio["metricas_consolidadas"]["falsos_cache_hits"])
        logger.info("   - Snapshots Prematuros:    %d", relatorio["metricas_consolidadas"]["snapshots_prematuros"])
        logger.info("   - Casos Nunca Quiescentes: %d", relatorio["metricas_consolidadas"]["casos_nunca_quiescentes"])
        logger.info("   Relatório salvo em: %s", caminho_rel)
        logger.info("================================================================================")
        return relatorio
    finally:
        oracle.paused = False

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(benchmark_quiescencia())
        except Exception as e:
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=30)
    if err:
        raise err[0]
