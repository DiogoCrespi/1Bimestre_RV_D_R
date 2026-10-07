import os
import sys
from typing import Dict, Any, List, Tuple, Optional
from typesafe_sdk import TypeSafeClient, Choice, Noul, Score

from rcs_client import RemoteControlClient

class JevGateway:
    """
    Gateway inteligente e Guardrail de decisões para agentes de código e automação de desktop.
    Utiliza o modelo Jev (TypeSafe System One) para roteamento ultra-rápido de ferramentas e auditoria de segurança.
    """
    def __init__(self, api_key: Optional[str] = None, rcs_url: Optional[str] = None):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise ValueError("TYPESAFE_API_KEY não configurada no ambiente ou no construtor.")
        os.environ["TYPESAFE_API_KEY"] = self.api_key

        self.client = TypeSafeClient()
        self.rcs = RemoteControlClient(rcs_url)

    def route_tool_call(
        self,
        user_intent: str,
        active_context: str,
        available_tools: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        GATE 1: Roteia o próximo passo do agente selecionando a ferramenta ideal.
        Retorna a ferramenta escolhida, o grau de confiança e as probabilidades.
        """
        tools = available_tools or {
            "rcs_hotkey_save": "Enviar atalho Ctrl+S para salvar código na janela ativa",
            "rcs_unity_play": "Acionar Play/Stop no editor Unity para testar jogo",
            "run_linter": "Executar verificação estática de sintaxe e estilo de código",
            "run_unit_tests": "Executar suíte de testes unitários",
            "escalate_to_reasoning": "Tarefa complexa que exige raciocínio profundo de arquitetura ou refatoração"
        }

        resp = self.client.system_one(
            state={
                "intencao_usuario": user_intent,
                "contexto_atual": active_context
            },
            questions={
                "tool_decision": Choice(
                    instructions="Qual ferramenta ou ação deve ser executada imediatamente para atender a intenção?",
                    criteria=tools
                )
            },
            model="jev-latest"
        )

        choice_ans = resp.choices["tool_decision"]
        return {
            "selected_tool": choice_ans.choice,
            "confidence": choice_ans.confidence,
            "probabilities": choice_ans.probabilities,
            "tokens_used": resp.usage.output_tokens if resp.usage else 0
        }

    def verify_safety_guardrail(
        self,
        planned_action: str,
        target_window: str,
        code_has_unsaved_changes: bool = False
    ) -> Tuple[bool, float]:
        """
        GATE 2: Trava de segurança (Guardrail).
        Avalia se a ação automatizada no desktop pode causar perda irreversível de dados.
        Retorna (autorizado: bool, probabilidade_segura: float).
        """
        resp = self.client.system_one(
            state={
                "acao_planejada": planned_action,
                "janela_alvo": target_window,
                "alteracoes_pendentes_sem_salvar": code_has_unsaved_changes
            },
            questions={
                "is_safe": Noul(
                    instructions="Esta ação física de automação é segura e não causará perda de código ou fechamento indevido de processos críticos?",
                    criteria={
                        "true": "Ação segura, idempotente ou com preservação de estado",
                        "false": "Ação destrutiva, arriscada ou com perda de trabalho não salvo"
                    }
                )
            },
            model="jev-latest"
        )

        prob_safe = float(resp.nouls["is_safe"].noul)
        authorized = prob_safe >= 0.85
        return authorized, prob_safe

    def evaluate_build_log(self, build_output: str) -> Dict[str, Any]:
        """
        GATE 3: Validação de desfecho (Outcome Verifier).
        Avalia o log de compilação da Unity / C# / Python em 3 níveis ordinais.
        """
        resp = self.client.system_one(
            state={"log_de_compilacao": build_output[:3000]},
            questions={
                "build_status": Score(
                    instructions="Avalie o status da compilação e execução a partir do log:",
                    criteria=[
                        "Erro crítico: compilação falhou ou exceção não tratada",
                        "Avisos: compilou com advertências (warnings/deprecations)",
                        "Sucesso limpo: compilação bem-sucedida sem erros"
                    ]
                )
            },
            model="jev-latest"
        )

        score_ans = resp.scores["build_status"]
        return {
            "score": score_ans.score,
            "confidence": score_ans.confidence,
            "probabilities": score_ans.probabilities
        }

    def execute_guarded_rcs_action(
        self,
        action_type: str,
        params: Dict[str, Any],
        context_description: str
    ) -> Dict[str, Any]:
        """
        Executa ação física via remote_control_server com chancela do Guardrail Jev.
        """
        # 1. Checagem do Guardrail
        authorized, prob = self.verify_safety_guardrail(
            planned_action=f"Tipo: {action_type} com parâmetros {params}",
            target_window=params.get("window", "Desktop"),
            code_has_unsaved_changes=params.get("unsaved_changes", False)
        )

        if not authorized:
            return {
                "ok": False,
                "blocked_by_guardrail": True,
                "safety_probability": prob,
                "message": f"Ação bloqueada pelo Guardrail Jev. Probabilidade de segurança ({prob:.2f}) abaixo do limiar (0.85)."
            }

        # 2. Execução física no RCS se o servidor estiver ativo
        if not self.rcs.is_alive():
            return {
                "ok": False,
                "blocked_by_guardrail": False,
                "safety_probability": prob,
                "server_offline": True,
                "message": "Ação aprovada pelo Jev, mas o remote_control_server não está respondendo em http://127.0.0.1:8765."
            }

        success = False
        if action_type == "hotkey":
            success = self.rcs.send_hotkey(params.get("keys", []))
        elif action_type == "click":
            success = self.rcs.click(params.get("x", 0), params.get("y", 0))
        elif action_type == "type":
            success = self.rcs.type_text(params.get("text", ""))
        elif action_type == "focus":
            success = self.rcs.focus_window(params.get("keyword", ""))

        return {
            "ok": success,
            "blocked_by_guardrail": False,
            "safety_probability": prob,
            "message": "Ação executada com sucesso pelo remote_control_server." if success else "Falha na execução do remote_control_server."
        }
