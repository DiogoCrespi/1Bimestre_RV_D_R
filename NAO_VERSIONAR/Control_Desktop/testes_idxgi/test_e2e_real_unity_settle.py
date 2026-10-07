import os
import sys
import time
import json
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    get_system_state,
    execute_system_action,
    _execute_system_action,
    get_dxgi_oracle,
    focus_window,
    get_foreground_window_info
)
import remote_control_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("e2e_unity_settle")

def run_e2e_real_unity():
    ensure_desktop_access()
    logger.info("======================================================================")
    logger.info("INICIANDO BATERIA E2E EM WINDOWS REAL: UNITY EDITOR 6.5 & DXGI SETTLE")
    logger.info("======================================================================")
    
    # 1. Ativar Oráculo DXGI
    remote_control_server.ENABLE_DXGI_ORACLE = True
    oracle = get_dxgi_oracle()
    assert oracle is not None, "DXGI Invalidation Oracle indisponível"
    time.sleep(0.5)
    
    # 2. Focar janela da Unity
    fw_unity = focus_window(title_kw="Unity")
    logger.info("Foco na Unity: %s", fw_unity)
    time.sleep(0.3)
    
    unity_info = get_foreground_window_info()
    assert unity_info and "unity" in unity_info.get("process", "").lower(), f"Unity não está em primeiro plano: {unity_info}"
    win_rect = [unity_info["left"], unity_info["top"], unity_info["left"] + unity_info["width"], unity_info["top"] + unity_info["height"]]
    logger.info("Janela-alvo Unity: HWND=%s Rect=%s", unity_info["hwnd"], win_rect)
    
    relatorio = {
        "unity_hwnd": unity_info["hwnd"],
        "unity_rect": win_rect,
        "unity_title": unity_info["title"],
        "scenarios": []
    }
    
    # Função auxiliar para executar um cenário e medir settle + state
    def executar_cenario(nome, desc, acao_fn):
        logger.info("\n--- CENÁRIO: %s (%s) ---", nome, desc)
        
        # Estado base pré-ação
        st_pre = get_system_state(mode="som")
        tags_pre_count = st_pre.get("count", 0)
        wm_pre = oracle.get_watermark()
        logger.info("  Pré-Ação: tags=%d, wm_pre=%d", tags_pre_count, wm_pre)
        
        # Executar ação
        t_action_start = time.time()
        acao_fn()
        t_action_end = time.time()
        
        # Settle monitorado (usando os parâmetros padrão de produção: quiescence=40ms, timeout=300ms)
        mudou, motivo, det = oracle.wait_for_settle(
            wm_pre,
            win_rect=win_rect,
            quiescence_window_ms=40,
            min_observe_ms=30,
            max_timeout_ms=300
        )
        t_settle_end = time.time()
        settle_time_ms = (t_settle_end - t_action_end) * 1000.0
        
        logger.info("  Settle Result: mudou=%s, motivo=%s, elapsed=%.1fms, silence=%.1fms, events=%s",
                    mudou, motivo, det.get("elapsed_ms", 0), det.get("silence_ms", 0), det.get("activity_events", 0))
        logger.info("  Flags: temporally_quiescent=%s, outcome_verified=%s, never_quiescent=%s",
                    det.get("temporally_quiescent", False), det.get("outcome_verified", False), det.get("never_quiescent", False))
        
        # Captura de /state imediata pós-settle
        t_st_post0 = time.time()
        st_post = get_system_state(mode="som")
        t_st_post1 = time.time()
        post_lat_ms = (t_st_post1 - t_st_post0) * 1000.0
        tags_post_count = st_post.get("count", 0)
        cached_post = st_post.get("cached_by_dxgi_oracle", False)
        
        logger.info("  Pós-Settle /state: lat=%.1fms, tags=%d, cached=%s",
                    post_lat_ms, tags_post_count, cached_post)
        
        # Captura de segundo /state imediato (com a tela parada após a ação, DEVE dar CACHE HIT se settled!)
        t_st_idle0 = time.time()
        st_idle = get_system_state(mode="som")
        t_st_idle1 = time.time()
        idle_lat_ms = (t_st_idle1 - t_st_idle0) * 1000.0
        cached_idle = st_idle.get("cached_by_dxgi_oracle", False)
        logger.info("  Idle Subsequente /state: lat=%.2fms, cached=%s, motivo=%s",
                    idle_lat_ms, cached_idle, st_idle.get("dxgi_oracle_reason"))
        
        cenario_res = {
            "cenario": nome,
            "descricao": desc,
            "settle": {
                "mudou": mudou,
                "motivo": motivo,
                "elapsed_ms": det.get("elapsed_ms", round(settle_time_ms, 1)),
                "silence_ms": det.get("silence_ms", 0),
                "activity_events": det.get("activity_events", 0),
                "temporally_quiescent": det.get("temporally_quiescent", False),
                "outcome_verified": det.get("outcome_verified", False),
                "never_quiescent": det.get("never_quiescent", False)
            },
            "post_state": {
                "tags_pre": tags_pre_count,
                "tags_post": tags_post_count,
                "cached": cached_post,
                "latency_ms": round(post_lat_ms, 1)
            },
            "idle_subsequent_state": {
                "cached": cached_idle,
                "latency_ms": round(idle_lat_ms, 2),
                "oracle_reason": st_idle.get("dxgi_oracle_reason")
            },
            "safe_invalidation_ok": not cached_post if mudou else True,
            "quiescent_settle_ok": det.get("temporally_quiescent", False)
        }
        relatorio["scenarios"].append(cenario_res)
        time.sleep(0.3)
        return cenario_res

    # -------------------------------------------------------------------------
    # CENÁRIO 1: Abrir menu real da Unity (Menu Help)
    # -------------------------------------------------------------------------
    def cenario_1_abrir_menu():
        # Clica no menu 'Help' na barra de menu da Unity
        _execute_system_action({"action": "click", "x": 611, "y": 77, "journal": False})
    executar_cenario("1_abrir_menu_help", "Abrir menu Help da Unity e aguardar popup", cenario_1_abrir_menu)
    
    # -------------------------------------------------------------------------
    # CENÁRIO 2: Fechar menu real da Unity via Escape
    # -------------------------------------------------------------------------
    def cenario_2_fechar_menu():
        _execute_system_action({"action": "key_combination", "keys": ["escape"], "journal": False})
    executar_cenario("2_fechar_menu_escape", "Fechar popup de menu via Escape", cenario_2_fechar_menu)
    
    # -------------------------------------------------------------------------
    # CENÁRIO 3: Alternar Foldout no Inspector da Unity
    # -------------------------------------------------------------------------
    def cenario_3_foldout_inspector():
        # Clica no cabeçalho do primeiro componente (ex: Transform) no Inspector [1421, 190]
        _execute_system_action({"action": "click", "x": 1421, "y": 190, "journal": False})
    executar_cenario("3_foldout_inspector", "Alternar recolhimento/expansão de foldout no Inspector", cenario_3_foldout_inspector)
    
    # -------------------------------------------------------------------------
    # CENÁRIO 4: Trocar Abas Inferiores (Project -> Console -> Project)
    # -------------------------------------------------------------------------
    def cenario_4a_trocar_aba_console():
        # Aba Console em [165, 741]
        _execute_system_action({"action": "click", "x": 165, "y": 741, "journal": False})
    executar_cenario("4a_trocar_aba_console", "Alternar para a aba Console", cenario_4a_trocar_aba_console)
    
    def cenario_4b_trocar_aba_project():
        # Aba Project em [57, 742]
        _execute_system_action({"action": "click", "x": 57, "y": 742, "journal": False})
    executar_cenario("4b_retornar_aba_project", "Retornar para a aba Project", cenario_4b_trocar_aba_project)
    
    # -------------------------------------------------------------------------
    # CENÁRIO 5: Trocar Abas Centrais (Scene -> Game -> Scene)
    # -------------------------------------------------------------------------
    def cenario_5a_trocar_aba_game():
        # Aba Game em [511, 149]
        _execute_system_action({"action": "click", "x": 511, "y": 149, "journal": False})
    executar_cenario("5a_trocar_aba_game", "Alternar para a aba Game", cenario_5a_trocar_aba_game)
    
    def cenario_5b_trocar_aba_scene():
        # Aba Scene em [460, 149]
        _execute_system_action({"action": "click", "x": 460, "y": 149, "journal": False})
    executar_cenario("5b_retornar_aba_scene", "Retornar para a aba Scene", cenario_5b_trocar_aba_scene)
    
    # -------------------------------------------------------------------------
    # CENÁRIO 6: Ação com Processamento / Salvar Cena (Ctrl+S)
    # -------------------------------------------------------------------------
    def cenario_6_salvar_cena():
        # Ctrl+S provoca gravação em disco e breve repaint de indicador de status
        _execute_system_action({"action": "key_combination", "keys": ["ctrl", "s"], "journal": False})
    executar_cenario("6_salvar_cena_processamento", "Salvar cena (Ctrl+S) com processamento de asset", cenario_6_salvar_cena)
    
    # -------------------------------------------------------------------------
    # CENÁRIO 7: Unity com Viewport Renderizando Continuamente (Play Mode)
    # -------------------------------------------------------------------------
    logger.info("\n--- CENÁRIO 7: Viewport com Renderização Contínua (Play Mode) ---")
    # Inicia Play Mode via Ctrl+P
    _execute_system_action({"action": "key_combination", "keys": ["ctrl", "p"], "journal": False})
    logger.info("  Play Mode ativado. Aguardando inicialização do Game Loop da Unity...")
    time.sleep(1.5)
    
    try:
        wm_play = oracle.get_watermark()
        # Durante Play Mode contínuo, o settle de uma ação ou verificação deve acusar atividade contínua
        mudou_play, motivo_play, det_play = oracle.wait_for_settle(
            wm_play,
            win_rect=win_rect,
            quiescence_window_ms=40,
            min_observe_ms=30,
            max_timeout_ms=250
        )
        logger.info("  Play Mode Settle: mudou=%s, motivo=%s, events=%d, never_quiescent=%s",
                    mudou_play, motivo_play, det_play.get("activity_events", 0), det_play.get("never_quiescent", False))
        
        # /state sob atividade contínua deve recusar cache e fazer Full Snapshot defensivo
        st_play = get_system_state(mode="som")
        logger.info("  Play Mode /state: cached=%s, tags=%d (Full Snapshot defensivo garantido)",
                    st_play.get("cached_by_dxgi_oracle", False), st_play.get("count", 0))
        
        cenario_7 = {
            "cenario": "7_viewport_renderizacao_continua",
            "descricao": "Unity em Play Mode com frames constantes na Viewport",
            "settle": {
                "mudou": mudou_play,
                "motivo": motivo_play,
                "never_quiescent": det_play.get("never_quiescent", False),
                "activity_events": det_play.get("activity_events", 0),
                "elapsed_ms": det_play.get("elapsed_ms", 0)
            },
            "post_state": {
                "cached": st_play.get("cached_by_dxgi_oracle", False),
                "tags": st_play.get("count", 0)
            },
            "safe_fallback_under_continuous_rendering": not st_play.get("cached_by_dxgi_oracle", False),
            "continuous_activity_detected": det_play.get("never_quiescent", False) or det_play.get("activity_events", 0) > 2
        }
        relatorio["scenarios"].append(cenario_7)
    finally:
        # Sair do Play Mode
        logger.info("  Desativando Play Mode (Ctrl+P)...")
        _execute_system_action({"action": "key_combination", "keys": ["ctrl", "p"], "journal": False})
        time.sleep(1.0)
    
    # 3. Salvar relatório JSON
    out_path = os.path.join(os.path.dirname(__file__), "relatorio_e2e_unity_settle.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    logger.info("\n======================================================================")
    logger.info("BATERIA E2E FINALIZADA COM SUCESSO! Relatório salvo em: %s", out_path)
    logger.info("======================================================================")
    return relatorio

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(run_e2e_real_unity())
        except Exception as e:
            logger.exception("Erro durante bateria E2E Unity")
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=120)
    if err:
        raise err[0]
