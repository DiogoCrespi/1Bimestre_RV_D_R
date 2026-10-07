# -*- coding: utf-8 -*-
"""
test_outcome_verifier_suite.py - Suíte Oficial de Testes do OutcomeVerifier

Cenários Obrigatórios Validados conforme Diretriz do ChatGPT:
1. expectativa comprovada -> verified
2. ausência após timeout -> unknown (ausência de prova NÃO vira prova de falha)
3. evidência contrária explícita -> failed (prova positiva contrária)
4. drift + ausência de evidência -> unknown (drift não altera status sozinho)
5. drift + evidência contrária -> failed (drift contextual mantido)
6. evidência ambígua -> unknown
7. expectativa cumprida sem grande mudança visual -> verified
8. pré-condição preventiva -> impede disparo indevido
"""

import sys
import os
import time
import json
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from outcome_verifier import OutcomeVerifier, STATUS_VERIFIED, STATUS_FAILED, STATUS_UNKNOWN

class MockServer:
    def __init__(self):
        self.current_snapshot = {
            "frame_id": "frame_100",
            "timestamp": time.time(),
            "elements": [
                {"id": 1, "name": "UsernameInput", "value": "admin", "center": [200, 300]},
                {"id": 2, "name": "SubmitButton", "value": None, "center": [200, 350]},
            ],
            "marks": [
                {"tag": 10, "text": "Save", "center": [100, 100], "box": [80, 90, 120, 110]},
                {"tag": 11, "text": "Cancel", "center": [150, 100], "box": [130, 90, 170, 110]},
            ],
            "window": {"hwnd": 67032, "title": "Unity Editor - SampleScene"}
        }
        self.active_window = {"hwnd": 67032, "title": "Unity Editor - SampleScene"}
        self.open_windows = [
            {"hwnd": 67032, "title": "Unity Editor - SampleScene"},
            {"hwnd": 66970, "title": "Google Chrome"}
        ]

    def read_state_snapshot(self):
        return self.current_snapshot

    def get_foreground_window_info(self):
        return self.active_window

    def get_open_windows(self):
        return self.open_windows

    def find_text_candidates(self, state, text_query, exact=False, region=None):
        matches = []
        for m in state.get("marks", []):
            t = m.get("text", "").lower()
            if (t == text_query) if exact else (text_query in t):
                matches.append(m)
        return matches

class TestOutcomeVerifierSuite(unittest.TestCase):
    def setUp(self):
        self.mock_server = MockServer()
        self.verifier = OutcomeVerifier(server_module=self.mock_server)

    # 1. Expectativa Comprovada -> VERIFIED
    def test_01_expectativa_comprovada_verified(self):
        self.mock_server.current_snapshot["marks"].append({
            "tag": 20, "text": "Build Success", "center": [300, 400], "box": [250, 390, 350, 410]
        })
        self.mock_server.current_snapshot["frame_id"] = "frame_101"
        
        expect = {"type": "text_appears", "target": "Build Success"}
        res = self.verifier.verify_outcome(expect, timeout_ms=100)
        
        self.assertEqual(res["status"], STATUS_VERIFIED)
        self.assertTrue(res["verified"])
        self.assertEqual(res["reason"], "target_text_observed_confidently")
        self.assertIn("found", res["evidence"])
        print("\n[TESTE 1 - VERIFIED]:\n", json.dumps(res, indent=2))

    # 2. Ausência após Timeout -> UNKNOWN (Não vira failed!)
    def test_02_ausencia_apos_timeout_unknown(self):
        # Texto não aparece antes do timeout. O sistema NÃO infere falha, reporta UNKNOWN.
        expect = {"type": "text_appears", "target": "TextoQueNaoApareceu"}
        res = self.verifier.verify_outcome(expect, timeout_ms=100)
        
        self.assertEqual(res["status"], STATUS_UNKNOWN)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "target_text_not_observed_within_timeout")
        print("\n[TESTE 2 - AUSÊNCIA APÓS TIMEOUT -> UNKNOWN]:\n", json.dumps(res, indent=2))

    # 3. Evidência Contrária Explícita -> FAILED
    def test_03_evidencia_contraria_explicita_failed(self):
        # Exemplo A: Esperava value_equals("superadmin"), mas o campo foi lido como "admin"
        expect_val = {"type": "value_equals", "target": "UsernameInput", "value": "superadmin"}
        res_val = self.verifier.verify_outcome(expect_val, timeout_ms=100)
        self.assertEqual(res_val["status"], STATUS_FAILED)
        self.assertFalse(res_val["verified"])
        self.assertEqual(res_val["reason"], "field_value_mismatch_contrary_evidence")
        self.assertEqual(res_val["evidence"]["actual"], "admin")
        print("\n[TESTE 3A - EVIDÊNCIA CONTRÁRIA (VALOR) -> FAILED]:\n", json.dumps(res_val, indent=2))

        # Exemplo B: Esperava modal_closed("Save Dialog"), mas a janela do modal continua aberta
        self.mock_server.open_windows.append({"hwnd": 9991, "title": "Save Dialog", "is_modal": True})
        expect_modal = {"type": "modal_closed", "target": "Save Dialog"}
        res_modal = self.verifier.verify_outcome(expect_modal, timeout_ms=100)
        self.assertEqual(res_modal["status"], STATUS_FAILED)
        self.assertFalse(res_modal["verified"])
        self.assertEqual(res_modal["reason"], "modal_still_open_contrary_evidence")
        print("\n[TESTE 3B - EVIDÊNCIA CONTRÁRIA (MODAL ABERTO) -> FAILED]:\n", json.dumps(res_modal, indent=2))

    # 4. Drift + Ausência de Evidência -> UNKNOWN
    def test_04_drift_mais_ausencia_evidencia_unknown(self):
        # Alvo sofreu drift de +70px no eixo Y
        self.mock_server.current_snapshot["elements"][1]["center"] = [200, 420]
        target_meta = {"id": 2, "text": "SubmitButton", "center": [200, 350]}
        
        # Expectativa de texto novo que não apareceu antes do timeout
        expect = {"type": "text_appears", "target": "ConfirmationBanner"}
        res = self.verifier.verify_outcome(expect, action_target_meta=target_meta, timeout_ms=100)
        
        # O status deve ser UNKNOWN (pois não há prova de falha, apenas timeout),
        # mas o drift deve estar plenamente registrado informativamente!
        self.assertEqual(res["status"], STATUS_UNKNOWN)
        self.assertFalse(res["verified"])
        self.assertIsNotNone(res["drift"])
        self.assertTrue(res["drift"]["drift_detected"])
        self.assertEqual(res["drift"]["dy"], 70)
        print("\n[TESTE 4 - DRIFT + AUSÊNCIA DE EVIDÊNCIA -> UNKNOWN]:\n", json.dumps(res, indent=2))

    # 5. Drift + Evidência Contrária Explícita -> FAILED
    def test_05_drift_mais_evidencia_contraria_failed(self):
        # Alvo sofreu drift
        self.mock_server.current_snapshot["elements"][1]["center"] = [200, 420]
        target_meta = {"id": 2, "text": "SubmitButton", "center": [200, 350]}
        
        # Mas há evidência contrária explícita: modal continua comprovadamente aberto!
        self.mock_server.open_windows.append({"hwnd": 9992, "title": "Unsaved Changes Dialog", "is_modal": True})
        expect = {"type": "modal_closed", "target": "Unsaved Changes Dialog"}
        res = self.verifier.verify_outcome(expect, action_target_meta=target_meta, timeout_ms=100)
        
        # O status deve ser FAILED por prova contrária, com o drift anexado
        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertFalse(res["verified"])
        self.assertEqual(res["reason"], "modal_still_open_contrary_evidence")
        self.assertIsNotNone(res["drift"])
        self.assertTrue(res["drift"]["drift_detected"])
        print("\n[TESTE 5 - DRIFT + EVIDÊNCIA CONTRÁRIA -> FAILED]:\n", json.dumps(res, indent=2))

    # 6. Evidência Ambígua -> UNKNOWN
    def test_06_evidencia_ambigua_unknown(self):
        self.mock_server.current_snapshot["marks"].append({
            "tag": 21, "text": "Save As", "center": [500, 100], "box": [480, 90, 520, 110]
        })
        expect = {"type": "text_appears", "target": "Save", "exact": False}
        res = self.verifier.verify_outcome(expect, timeout_ms=100)
        
        self.assertEqual(res["status"], STATUS_UNKNOWN)
        self.assertFalse(res["verified"])
        self.assertIn("ambiguous_evidence", res["reason"])
        print("\n[TESTE 6 - EVIDÊNCIA AMBÍGUA -> UNKNOWN]:\n", json.dumps(res, indent=2))

    # 7. Expectativa Cumprida sem Grande Mudança Visual -> VERIFIED
    def test_07_expectativa_cumprida_sem_grande_mudanca_visual_verified(self):
        # Apenas um campo teve seu valor modificado via UIA
        self.mock_server.current_snapshot["elements"][0]["value"] = "novo_token_123"
        expect = {"type": "value_equals", "target": "UsernameInput", "value": "novo_token_123"}
        res = self.verifier.verify_outcome(expect, timeout_ms=100)
        
        self.assertEqual(res["status"], STATUS_VERIFIED)
        self.assertTrue(res["verified"])
        self.assertEqual(res["evidence"]["value"], "novo_token_123")
        print("\n[TESTE 7 - MICRO-MUDANÇA SEMÂNTICA -> VERIFIED]:\n", json.dumps(res, indent=2))

    # 8. Pré-condição Preventiva
    def test_08_precondition_evaluation(self):
        pre_ok = {"type": "value_equals", "target": "UsernameInput", "value": "admin"}
        ok, reason, ev = self.verifier.evaluate_precondition(pre_ok)
        self.assertTrue(ok)
        
        pre_fail = {"type": "value_equals", "target": "UsernameInput", "value": "wrong_user"}
        ok_f, reason_f, ev_f = self.verifier.evaluate_precondition(pre_fail)
        self.assertFalse(ok_f)
        self.assertEqual(reason_f, "precondition_value_mismatch")

if __name__ == "__main__":
    unittest.main()
