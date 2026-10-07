# -*- coding: utf-8 -*-
"""
test_e2e_outcome_verifier.py - Teste de Integração Real do OutcomeVerifier no Servidor
"""

import sys
import os
import time
import json

sys.path.insert(0, os.path.dirname(__file__))
import remote_control_server as rcs
from outcome_verifier import OutcomeVerifier, STATUS_VERIFIED, STATUS_FAILED, STATUS_UNKNOWN

def run_tests():
    rcs.ensure_desktop_access()
    verifier = OutcomeVerifier(server_module=rcs)
    
    print("======================================================================")
    print("TESTE DE CONTRATO E VERIFICADORES DO OUTCOMEVERIFIER")
    print("======================================================================")
    
    # 1. Teste de window_focused com janela ativa real (Unity HWND 67032)
    print("\n1. Testando window_focused (janela Unity 67032):")
    res1 = verifier.verify_outcome({"type": "window_focused", "target": 67032}, timeout_ms=500)
    print("Resultado:", json.dumps(res1, indent=2))
    assert res1["status"] in (STATUS_VERIFIED, STATUS_FAILED), "Deveria retornar status válido"
    
    # 2. Teste de window_focused falho com evidência contrária (janela inexistente)
    print("\n2. Testando window_focused FAILED (janela inexistente 999999):")
    res2 = verifier.verify_outcome({"type": "window_focused", "target": 999999}, timeout_ms=200)
    print("Resultado:", json.dumps(res2, indent=2))
    assert res2["status"] == STATUS_FAILED, "Deveria ser failed"
    assert not res2["verified"]
    assert "actual_focused" in res2["evidence"]
    
    # 3. Teste de modal_opened FAILED com evidência contrária (procura modal inexistente)
    print("\n3. Testando modal_opened FAILED (ModalInexistenteXYZ):")
    res3 = verifier.verify_outcome({"type": "modal_opened", "target": "ModalInexistenteXYZ"}, timeout_ms=200)
    print("Resultado:", json.dumps(res3, indent=2))
    assert res3["status"] == STATUS_FAILED, "Deveria ser failed"
    assert not res3["verified"]
    
    # 4. Teste de modal_closed VERIFIED (procura se modal fechou quando ele não existe)
    print("\n4. Testando modal_closed VERIFIED (ModalInexistenteXYZ):")
    res4 = verifier.verify_outcome({"type": "modal_closed", "target": "ModalInexistenteXYZ"}, timeout_ms=200)
    print("Resultado:", json.dumps(res4, indent=2))
    assert res4["status"] == STATUS_VERIFIED, "Deveria ser verified"
    assert res4["verified"]
    
    # 5. Teste de pré-condição opcional satisfeita
    print("\n5. Testando pré-condição de janela focada:")
    rcs.focus_window(hwnd=67032)
    time.sleep(0.3)
    fw = rcs.get_foreground_window_info() or {}
    pre_spec = {"type": "window_focused", "target": fw.get("hwnd")}
    ok, reason, ev = verifier.evaluate_precondition(pre_spec)
    print(f"Pré-condição: ok={ok}, reason={reason}, fw={fw.get('title')}")
    assert ok, "Deveria satisfazer pré-condição"
    
    # 6. Teste de pré-condição violada (impede ação indevida)
    pre_fail = {"type": "window_focused", "target": 888888}
    ok_f, reason_f, ev_f = verifier.evaluate_precondition(pre_fail)
    print(f"Pré-condição violada: ok={ok_f}, reason={reason_f}")
    assert not ok_f, "Deveria reprovar pré-condição violada"
    
    print("\nTODOS OS TESTES DE INTEGRAÇÃO PASSARAM COM SUCESSO!")

if __name__ == "__main__":
    run_tests()
