# -*- coding: utf-8 -*-
"""
outcome_verifier.py - Camada Genérica de Verificação Pós-Ação (OutcomeVerifier)

Princípios Arquiteturais e Regras de Segurança:
1. ok: indica que a requisição foi processada sem falhas catastróficas.
2. actionDispatched: indica unicamente que o input de baixo nível foi enviado ao SO.
3. outcomeVerified == True SOMENTE quando outcome.status == "verified".
4. failed: exige evidência contrária REAL observada no estado pós-ação.
5. unknown: emitido em caso de timeout, falta de evidência ou divergência ambígua.
   NUNCA tratar unknown como falha de execução nem como autorização para retry.
6. DXGI / Quiescência: oráculo de temporização para saber QUANDO observar. Não afere intenção.
7. O verifier NÃO executa retry nem repete ações.
8. Re-mira informativa: detecta e reporta drift de posição sem disparar nova ação.
9. Toda evidência é estritamente vinculada a frame_id ou timestamp fresco posterior à ação.
"""

import time
import logging
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("outcome_verifier")

STATUS_VERIFIED = "verified"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

class OutcomeVerifier:
    def __init__(self, server_module=None):
        self.server = server_module
        if self.server is None:
            try:
                import remote_control_server as rcs
                self.server = rcs
            except ImportError:
                pass
        if self.server and hasattr(self.server, "ensure_desktop_access"):
            try:
                self.server.ensure_desktop_access()
            except Exception:
                pass

    def evaluate_precondition(self, precondition: Optional[Dict[str, Any]], current_state: Optional[Dict[str, Any]] = None) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Avalia se a pré-condição declarada pelo chamador é válida antes da ação.
        Retorna: (is_satisfied, reason, evidence)
        """
        if not precondition:
            return True, "no_precondition", {}
            
        p_type = precondition.get("type")
        target = precondition.get("target")
        expected_val = precondition.get("value")
        
        if not current_state and self.server:
            current_state = self.server.read_state_snapshot()
            
        if p_type == "value_equals":
            # Checa se o campo possui o valor inicial esperado
            actual_val = self._query_field_value(target, current_state)
            if actual_val is None:
                return False, "precondition_target_not_found", {"target": target}
            matches = (str(actual_val).strip() == str(expected_val).strip())
            return matches, ("precondition_satisfied" if matches else "precondition_value_mismatch"), {
                "target": target, "expected": expected_val, "actual": actual_val
            }
            
        elif p_type in ("window_focused", "window_active"):
            active = self._query_active_window()
            hw = active.get("hwnd")
            title = active.get("title", "")
            matches = False
            if isinstance(target, int) and hw == target:
                matches = True
            elif isinstance(target, str) and target.lower() in title.lower():
                matches = True
            return matches, ("precondition_satisfied" if matches else "window_not_focused"), {
                "expected": target, "active_window": active
            }
            
        return True, "unsupported_precondition_ignored", {}

    def verify_outcome(self, expect: Dict[str, Any], pre_state: Optional[Dict[str, Any]] = None, 
                       action_target_meta: Optional[Dict[str, Any]] = None,
                       timeout_ms: float = 1500.0) -> Dict[str, Any]:
        """
        Executa a verificação pós-ação baseada no contrato de expectativa.
        Retorna o objeto de resultado estrito:
        {
            "status": "verified" | "failed" | "unknown",
            "verified": bool,
            "verifier": str,
            "evidence": dict,
            "drift": dict | None,
            "elapsed_ms": float,
            "reason": str
        }
        """
        t0 = time.time()
        timeout_s = max(0.1, timeout_ms / 1000.0)
        v_type = expect.get("type", "")
        target = expect.get("target")
        
        # 1. Roteamento para o verificador especializado
        if v_type in ("text_appears", "text_disappears"):
            result = self._verify_text_presence(v_type, target, expect, pre_state, timeout_s)
        elif v_type in ("modal_opened", "modal_closed"):
            result = self._verify_modal_state(v_type, target, expect, pre_state, timeout_s)
        elif v_type in ("window_focused", "window_active"):
            result = self._verify_window_focused(target, expect, timeout_s)
        elif v_type == "value_equals":
            result = self._verify_value_equals(target, expect.get("value"), expect, pre_state, timeout_s)
        else:
            result = {
                "status": STATUS_UNKNOWN,
                "verified": False,
                "reason": f"unsupported_expect_type_{v_type}",
                "evidence": {}
            }
            
        # 2. Avaliação de Drift de Mira (Informativa, sem repetir ação)
        drift_info = None
        if action_target_meta and result["status"] in (STATUS_FAILED, STATUS_UNKNOWN):
            drift_info = self._check_target_drift(action_target_meta)
            
        elapsed_ms = round((time.time() - t0) * 1000.0, 2)
        
        return {
            "status": result["status"],
            "verified": (result["status"] == STATUS_VERIFIED),
            "verifier": v_type,
            "reason": result.get("reason", ""),
            "evidence": result.get("evidence", {}),
            "drift": drift_info,
            "elapsed_ms": elapsed_ms
        }

    # =========================================================================
    # Verificador 1: text_appears / text_disappears
    # =========================================================================
    def _verify_text_presence(self, v_type: str, target: str, expect: Dict[str, Any], 
                              pre_state: Optional[Dict[str, Any]], timeout_s: float) -> Dict[str, Any]:
        target_str = str(target or "").strip().lower()
        exact = bool(expect.get("exact", False))
        region = expect.get("region")
        start_t = time.time()
        
        while time.time() - start_t <= timeout_s:
            # Obtém snapshot atual fresco
            fresh_state = self._get_fresh_snapshot()
            candidates = self._find_candidates_in_state(fresh_state, target_str, exact, region)
            
            if v_type == "text_appears":
                if len(candidates) == 1:
                    return {
                        "status": STATUS_VERIFIED,
                        "reason": "target_text_observed_confidently",
                        "evidence": {"found": candidates[0], "frame_id": fresh_state.get("frame_id")}
                    }
                elif len(candidates) > 1:
                    # Evidência ambígua
                    return {
                        "status": STATUS_UNKNOWN,
                        "reason": f"ambiguous_evidence_{len(candidates)}_candidates_found",
                        "evidence": {"candidate_count": len(candidates), "candidates": candidates, "frame_id": fresh_state.get("frame_id")}
                    }
            elif v_type == "text_disappears":
                if len(candidates) == 0:
                    return {
                        "status": STATUS_VERIFIED,
                        "reason": "target_text_confirmed_absent",
                        "evidence": {"target": target_str, "frame_id": fresh_state.get("frame_id")}
                    }
                    
            time.sleep(0.05)
            
        # Esgotou timeout
        fresh_state = self._get_fresh_snapshot()
        candidates = self._find_candidates_in_state(fresh_state, target_str, exact, region)
        
        if v_type == "text_appears":
            # Ausência após timeout: NÃO é falha provada, é UNKNOWN
            return {
                "status": STATUS_UNKNOWN,
                "reason": "target_text_not_observed_within_timeout",
                "evidence": {"searched_target": target_str, "frame_id": fresh_state.get("frame_id")}
            }
        else: # text_disappears
            if len(candidates) > 0:
                # Evidência contrária explícita: o texto ainda está presente com certeza
                return {
                    "status": STATUS_FAILED,
                    "reason": "text_still_present_contrary_evidence",
                    "evidence": {"remaining_candidates": candidates, "frame_id": fresh_state.get("frame_id")}
                }
            return {
                "status": STATUS_UNKNOWN,
                "reason": "disappearance_verification_timeout",
                "evidence": {"frame_id": fresh_state.get("frame_id")}
            }

    # =========================================================================
    # Verificador 2: modal_opened / modal_closed
    # =========================================================================
    def _verify_modal_state(self, v_type: str, target: Any, expect: Dict[str, Any],
                            pre_state: Optional[Dict[str, Any]], timeout_s: float) -> Dict[str, Any]:
        target_name = str(target or "").strip().lower()
        start_t = time.time()
        
        while time.time() - start_t <= timeout_s:
            # 1. Prioridade UIA / Win32: inspeciona janelas ativas e filhas modais
            active_win = self._query_active_window()
            all_wins = self._query_open_windows()
            
            # Identifica se há modal ativo
            modal_win = None
            for w in all_wins:
                w_title = w.get("title", "").lower()
                is_dialog = ("dialog" in w_title or "modal" in w_title or w.get("is_modal"))
                if target_name:
                    if target_name in w_title:
                        modal_win = w
                        break
                elif is_dialog:
                    modal_win = w
                    break
                    
            if v_type == "modal_opened":
                if modal_win:
                    return {
                        "status": STATUS_VERIFIED,
                        "reason": "modal_detected_and_active",
                        "evidence": {"modal_window": modal_win}
                    }
            elif v_type == "modal_closed":
                if not modal_win:
                    return {
                        "status": STATUS_VERIFIED,
                        "reason": "modal_confirmed_closed_absent",
                        "evidence": {"active_window": active_win}
                    }
                    
            time.sleep(0.05)
            
        # Timeout atingido
        active_win = self._query_active_window()
        all_wins = self._query_open_windows()
        modal_still_present = any(target_name in w.get("title", "").lower() for w in all_wins) if target_name else False
        
        if v_type == "modal_opened":
            # Modal esperado NÃO apareceu antes do timeout: NÃO é falha provada, é UNKNOWN
            return {
                "status": STATUS_UNKNOWN,
                "reason": "modal_did_not_appear_within_timeout",
                "evidence": {"searched_modal": target_name, "active_window": active_win}
            }
        else: # modal_closed
            if modal_still_present:
                # Evidência contrária explícita: o modal continua comprovadamente aberto
                return {
                    "status": STATUS_FAILED,
                    "reason": "modal_still_open_contrary_evidence",
                    "evidence": {"modal_target": target_name, "active_window": active_win}
                }
            return {
                "status": STATUS_UNKNOWN,
                "reason": "modal_state_inconclusive_timeout",
                "evidence": {"active_window": active_win}
            }

    # =========================================================================
    # Verificador 3: window_focused
    # =========================================================================
    def _verify_window_focused(self, target: Any, expect: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        start_t = time.time()
        while time.time() - start_t <= timeout_s:
            active = self._query_active_window()
            hw = active.get("hwnd")
            title = active.get("title", "").lower()
            
            matches = False
            if isinstance(target, int) and hw == target:
                matches = True
            elif isinstance(target, str) and str(target).lower() in title:
                matches = True
                
            if matches:
                return {
                    "status": STATUS_VERIFIED,
                    "reason": "window_focus_confirmed",
                    "evidence": {"focused_window": active}
                }
            time.sleep(0.04)
            
        active = self._query_active_window()
        actual_hw = active.get("hwnd")
        if actual_hw and actual_hw != target:
            # Evidência contrária real: outra janela confirmada em primeiro plano
            return {
                "status": STATUS_FAILED,
                "reason": "different_window_focused_contrary_evidence",
                "evidence": {"expected": target, "actual_focused": active}
            }
        return {
            "status": STATUS_UNKNOWN,
            "reason": "focused_window_could_not_be_determined",
            "evidence": {"expected": target, "actual_focused": active}
        }

    # =========================================================================
    # Verificador 4: value_equals (para type_into)
    # =========================================================================
    def _verify_value_equals(self, target: Any, expected_val: Any, expect: Dict[str, Any],
                             pre_state: Optional[Dict[str, Any]], timeout_s: float) -> Dict[str, Any]:
        start_t = time.time()
        exp_str = str(expected_val)
        
        while time.time() - start_t <= timeout_s:
            fresh_state = self._get_fresh_snapshot()
            actual_val = self._query_field_value(target, fresh_state)
            
            if actual_val is not None:
                if str(actual_val).strip() == exp_str.strip():
                    return {
                        "status": STATUS_VERIFIED,
                        "reason": "field_value_matches_expected",
                        "evidence": {"target": target, "value": actual_val, "frame_id": fresh_state.get("frame_id")}
                    }
            time.sleep(0.05)
            
        fresh_state = self._get_fresh_snapshot()
        actual_val = self._query_field_value(target, fresh_state)
        if actual_val is not None:
            return {
                "status": STATUS_FAILED,
                "reason": "field_value_mismatch_contrary_evidence",
                "evidence": {"target": target, "expected": exp_str, "actual": actual_val, "frame_id": fresh_state.get("frame_id")}
            }
            
        return {
            "status": STATUS_UNKNOWN,
            "reason": "field_value_could_not_be_read_unknown",
            "evidence": {"target": target, "expected": exp_str, "frame_id": fresh_state.get("frame_id")}
        }

    # =========================================================================
    # Re-mira Informativa (Target Drift Detection)
    # =========================================================================
    def _check_target_drift(self, action_target_meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Checa se o elemento que foi alvo da ação mudou de posição entre o agendamento
        e a verificação. NÃO repete a ação.
        """
        orig_center = action_target_meta.get("center")
        target_text = action_target_meta.get("text")
        target_id = action_target_meta.get("id")
        target_tag = action_target_meta.get("tag")
        
        if not orig_center:
            return None
            
        fresh_state = self._get_fresh_snapshot()
        new_candidate = None
        
        # 1. Procura por ID ou Tag no novo estado
        if target_id:
            for elem in fresh_state.get("elements", []):
                if elem.get("id") == target_id:
                    new_candidate = elem
                    break
        if not new_candidate and target_tag:
            for m in fresh_state.get("marks", []):
                if m.get("tag") == target_tag:
                    new_candidate = m
                    break
        if not new_candidate and target_text:
            candidates = self._find_candidates_in_state(fresh_state, str(target_text).lower(), exact=False)
            if len(candidates) == 1:
                new_candidate = candidates[0]
                
        if new_candidate and "center" in new_candidate:
            nc = new_candidate["center"]
            dx = nc[0] - orig_center[0]
            dy = nc[1] - orig_center[1]
            dist = (dx**2 + dy**2) ** 0.5
            if dist > 5.0: # Drift significativo detectado
                return {
                    "drift_detected": True,
                    "original_center": orig_center,
                    "new_center": nc,
                    "drift_distance_px": round(dist, 1),
                    "dx": dx,
                    "dy": dy,
                    "target_identity": target_text or target_id or target_tag
                }
                
        return None

    # =========================================================================
    # Helpers de Consulta de Estado
    # =========================================================================
    def _get_fresh_snapshot(self) -> Dict[str, Any]:
        if self.server and hasattr(self.server, "read_state_snapshot"):
            return self.server.read_state_snapshot()
        return {}

    def _query_active_window(self) -> Dict[str, Any]:
        # Sem reserva para a janela do ultimo snapshot: e dado velho, e com ele o
        # verificador certificaria foco (verified) ou acusaria outra janela
        # (failed) sem evidencia atual - o contrato manda devolver unknown. A
        # causa do "thread de console retorna 0" foi corrigida na origem:
        # get_foreground_window_info anexa ao desktop antes de perguntar.
        if self.server and hasattr(self.server, "get_foreground_window_info"):
            fw = self.server.get_foreground_window_info()
            if fw and fw.get("hwnd"):
                return fw
        return {}

    def _query_open_windows(self) -> List[Dict[str, Any]]:
        if self.server and hasattr(self.server, "get_open_windows"):
            return self.server.get_open_windows() or []
        return []

    def _find_candidates_in_state(self, state: Dict[str, Any], text_query: str, exact: bool = False, region=None) -> List[Dict[str, Any]]:
        if self.server and hasattr(self.server, "find_text_candidates"):
            return self.server.find_text_candidates(state, text_query, exact=exact, region=region)
        # Fallback local se o servidor não estiver conectado
        matches = []
        for m in state.get("marks", []):
            t = m.get("text", "").lower()
            if (t == text_query) if exact else (text_query in t):
                matches.append(m)
        return matches

    def _query_field_value(self, target: Any, state: Dict[str, Any]) -> Optional[str]:
        # Consulta UIA primeiro
        target_str = str(target).lower()
        for elem in state.get("elements", []):
            if elem.get("id") == target or target_str in elem.get("name", "").lower():
                val = elem.get("value") or elem.get("text")
                if val is not None:
                    return str(val)
        # Consulta marcas OCR na vizinhança do target
        for m in state.get("marks", []):
            if target_str in m.get("text", "").lower():
                return m.get("text")
        return None
