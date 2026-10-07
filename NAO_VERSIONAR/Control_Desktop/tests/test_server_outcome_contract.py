# -*- coding: utf-8 -*-
"""
test_server_outcome_contract.py - Validação do Contrato do OutcomeVerifier via execute_system_action
"""

import sys
import os
import json
import ctypes

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import remote_control_server as rcs


def mover_sem_deslocar():
    """Coordenadas de um "move" que despacha entrada real sem mexer no mouse.

    O contrato precisa de uma acao que passe pelo SendInput, para que
    actionDispatched seja MEDIDO como True. Mover para onde o cursor ja esta
    cumpre isso sem tirar o ponteiro do usuario do lugar. Sem "monitor", o
    servidor trata x/y como coordenada absoluta da tela - a mesma do
    GetCursorPos.
    """
    pt = rcs.POINT(0, 0)
    rcs.user32.GetCursorPos(ctypes.byref(pt))
    return {"action": "move", "x": pt.x, "y": pt.y}


def test_contract():
    rcs.ensure_desktop_access()
    print("======================================================================")
    print("VALIDAÇÃO DO CONTRATO DE OUTCOMEVERIFIER VIA EXECUTE_SYSTEM_ACTION")
    print("======================================================================")

    # 1. Ação com pré-condição violada (rejeição segura sem despachar ação)
    req_pre_fail = {
        "action": "click",
        "x": 100,
        "y": 100,
        "precondition": {
            "type": "window_focused",
            "target": 99999999 # HWND inexistente
        }
    }
    res1 = rcs.execute_system_action(req_pre_fail)
    print("\n1. Teste de Pré-Condição Violada:")
    print(json.dumps(res1, indent=2))
    assert res1["ok"] is False
    assert res1["actionDispatched"] is False
    assert res1["outcomeVerified"] is False
    assert res1["outcome"]["status"] == "failed"
    assert res1["error_code"] == "precondition_failed"

    # 2. Ação com Expectativa Satisfeita (VERIFIED)
    # O alvo e descoberto na hora. HWND fixo nao serve: o Windows reaproveita
    # handles, e o 67032 que era o Unity numa sessao virou depois a "Default IME"
    # invisivel de outro processo - o teste passava a focar essa janela oculta.
    # Focar a janela que ja esta em primeiro plano nao rouba o foco do usuario.
    alvo = rcs.get_foreground_window_info() or next(
        (w for w in rcs.get_open_windows() if rcs.janela_utilizavel(w)), {})
    assert alvo.get("hwnd"), "nenhuma janela visivel para o teste de foco"
    req_verified = {
        "action": "focus",
        "hwnd": alvo["hwnd"],
        "expect": {
            "type": "window_focused",
            "target": alvo["hwnd"],
            "timeout_ms": 300
        }
    }
    res2 = rcs.execute_system_action(req_verified)
    print("\n2. Teste de Expectativa Satisfeita (VERIFIED):")
    print(json.dumps(res2, indent=2))
    assert res2["actionDispatched"] is True
    assert res2["outcomeVerified"] is True
    assert res2["outcome"]["status"] == "verified"

    # 3. Ação com Expectativa Violada com Evidência Contrária Explícita (FAILED)
    # Exemplo: a janela do passo 2 esta confirmadamente em foco, mas esperamos 12345
    req_failed = {
        **mover_sem_deslocar(),
        "expect": {
            "type": "window_focused",
            "target": 12345,
            "timeout_ms": 200
        }
    }
    res3 = rcs.execute_system_action(req_failed)
    print("\n3. Teste de Evidência Contrária Explícita (FAILED):")
    print(json.dumps(res3, indent=2))
    assert res3["actionDispatched"] is True
    assert res3["outcomeVerified"] is False
    assert res3["outcome"]["status"] == "failed"
    assert res3["outcome"]["reason"] == "different_window_focused_contrary_evidence"

    # 4. Ação com Ausência após Timeout (UNKNOWN - ausência de prova NÃO vira falha)
    req_unknown_modal = {
        **mover_sem_deslocar(),
        "expect": {
            "type": "modal_opened",
            "target": "ModalQueNaoExiste",
            "timeout_ms": 200
        }
    }
    res_unk = rcs.execute_system_action(req_unknown_modal)
    print("\n4. Teste de Ausência após Timeout (UNKNOWN):")
    print(json.dumps(res_unk, indent=2))
    assert res_unk["actionDispatched"] is True
    assert res_unk["outcomeVerified"] is False
    assert res_unk["outcome"]["status"] == "unknown"
    assert res_unk["outcome"]["reason"] == "modal_did_not_appear_within_timeout"

    # 4. Ação com Timeout / Inconclusivo (UNKNOWN)
    # Exemplo: digitação onde o campo não pode ser lido ou é inacessível
    req_unknown = {
        **mover_sem_deslocar(),
        "expect": {
            "type": "value_equals",
            "target": "CampoInexistente123",
            "value": "teste",
            "timeout_ms": 150
        }
    }
    res4 = rcs.execute_system_action(req_unknown)
    print("\n4. Teste de Timeout / Sem Evidência (UNKNOWN):")
    print(json.dumps(res4, indent=2))
    assert res4["actionDispatched"] is True
    assert res4["outcomeVerified"] is False
    assert res4["outcome"]["status"] == "unknown"

    print("\n======================================================================")
    print("TODAS AS ASSERÇÕES DO CONTRATO PASSARAM COM 100% DE CONFORMIDADE!")
    print("======================================================================")

if __name__ == "__main__":
    test_contract()
