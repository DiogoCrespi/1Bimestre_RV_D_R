import os
import sys
import time
import json
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    run_in_desktop_thread,
    get_system_state,
    execute_system_action,
    get_dxgi_oracle,
    publish_state_snapshot
)
import remote_control_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_e2e_oracle")

def testar_e2e_oracle():
    ensure_desktop_access()
    logger.info("=== INICIANDO TESTE E2E DO ORÁCULO DXGI NO SERVIDOR ===")
    
    # 1. Habilitar o oráculo explicitamente (opt-in)
    remote_control_server.ENABLE_DXGI_ORACLE = True
    oracle = get_dxgi_oracle()
    assert oracle is not None, "Falha ao obter instância do Oráculo DXGI"
    time.sleep(0.4)
    
    # 2. Capturar primeiro estado (deve ser Full Snapshot pois não há cache)
    t0 = time.time()
    st1 = get_system_state(mode="som")
    t1 = (time.time() - t0) * 1000.0
    logger.info("1. Primeiro /state (Full Snapshot): %.1fms | tags: %d | cached=%s",
                t1, st1.get("count", 0), st1.get("cached_by_dxgi_oracle", False))
    assert not st1.get("cached_by_dxgi_oracle"), "Primeiro estado não deveria ser cache hit"
    
    # 3. Capturar segundo estado imediatamente sem nenhuma ação (deve ser Cache Hit via Oráculo!)
    t0 = time.time()
    st2 = get_system_state(mode="som")
    t2 = (time.time() - t0) * 1000.0
    logger.info("2. Segundo /state (Tela Parada - Oracle Check): %.2fms | tags: %d | cached=%s | motivo=%s",
                t2, st2.get("count", 0), st2.get("cached_by_dxgi_oracle", False), st2.get("dxgi_oracle_reason"))
    assert st2.get("cached_by_dxgi_oracle") is True, "Segundo estado deveria ser CACHE HIT via Oráculo DXGI"
    assert t2 < 15.0, f"Latência de cache hit deveria ser < 15ms, foi {t2:.1f}ms"
    
    # 4. Executar uma ação real pelo dispatcher do servidor com settle pós-ação
    logger.info("3. Disparando ação (scroll) via execute_system_action...")
    act_res = execute_system_action({
        "action": "scroll",
        "dy": -120,
        "x": 600,
        "y": 400
    })
    logger.info("   Ação concluída: %s", act_res.get("ok"))
    
    # 5. Capturar terceiro estado após ação (o Oráculo deve detectar a mudança e forçar Full Snapshot!)
    t0 = time.time()
    st3 = get_system_state(mode="som")
    t3 = (time.time() - t0) * 1000.0
    logger.info("4. Terceiro /state (Pós-Ação): %.1fms | tags: %d | cached=%s",
                t3, st3.get("count", 0), st3.get("cached_by_dxgi_oracle", False))
    assert not st3.get("cached_by_dxgi_oracle"), "Estado pós-ação deve sofrer invalidação e gerar Full Snapshot"
    
    # 6. Teste de Fallback com janela fora da tela / inconsistência
    logger.info("5. Testando fallback defensivo com janela simulada fora da tela...")
    with remote_control_server.state_cache_lock:
        remote_control_server.current_state_cache["window"] = {"hwnd": 999999, "rect": [-2000, -2000, -1000, -1000]}
    
    # Próxima chamada deve rejeitar o cache (hwnd diferente / fora da tela) e forçar full snapshot
    st4 = get_system_state(mode="som")
    logger.info("   Resultado fallback: cached=%s (deve ser False)", st4.get("cached_by_dxgi_oracle", False))
    assert not st4.get("cached_by_dxgi_oracle"), "Janela fora da tela ou inconsistente deve recusar cache"
    
    logger.info("=== TODOS OS TESTES E2E DO ORÁCULO DXGI FORAM VALIDADOS COM SUCESSO! ===")
    return {
        "ok": True,
        "latencia_full_ms": round(t1, 1),
        "latencia_cache_hit_ms": round(t2, 2),
        "aceleracao_fator": round(t1 / max(0.1, t2), 1),
        "invalidação_pos_acao_ok": not st3.get("cached_by_dxgi_oracle"),
        "fallback_defensivo_ok": True
    }

if __name__ == "__main__":
    import threading
    res = []
    err = []
    def _runner():
        ensure_desktop_access()
        try:
            res.append(testar_e2e_oracle())
        except Exception as e:
            err.append(e)
        finally:
            release_desktop_access()
            
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=30)
    if err:
        raise err[0]
