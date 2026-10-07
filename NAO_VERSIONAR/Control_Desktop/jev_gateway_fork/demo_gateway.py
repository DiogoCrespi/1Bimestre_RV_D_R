import os
import sys

# Inclusão de src
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from jev_router import JevGateway

def main():
    print("=" * 70)
    print(" JEV GATEWAY & GUARDRAIL - FORK DE DESENVOLVIMENTO E AUTOMAÇÃO")
    print(" Modelo: Jev (TypeSafe System One) | Atuador: remote_control_server.py")
    print("=" * 70)

    # Inicializar o Gateway com a API Key configurada
    api_key = "apikey_21645da99ca7b663484083f62d43f1f5fe2a_9bd6c2552575569430553f06384bcf70cb40b73c14ba9862599f0ce4c9aea64c"
    gw = JevGateway(api_key=api_key)

    scenarios = [
        {
            "titulo": "Cenário 1: Intenção de Programação Rotineira (Salvar Código)",
            "intent": "Terminei a edição do script do Player em C#. Salve o arquivo para compilar.",
            "context": "Visual Studio Code aberto em PlayerMovement.cs com alterações pendentes.",
            "action_spec": {"type": "hotkey", "params": {"keys": ["Ctrl", "S"], "window": "Visual Studio Code", "unsaved_changes": True}}
        },
        {
            "titulo": "Cenário 2: Intenção Potencialmente Perigosa (Forçar Encerramento)",
            "intent": "Feche a janela da Unity imediatamente e mate o processo.",
            "context": "Unity Editor em execução com cena não salva (MainScene*).",
            "action_spec": {"type": "hotkey", "params": {"keys": ["Alt", "F4"], "window": "Unity - MainScene*", "unsaved_changes": True}}
        },
        {
            "titulo": "Cenário 3: Solicitação de Refatoração Arquitetural Profunda",
            "intent": "Quero redesenhar o sistema de inventário para usar arquitetura orientada a dados com zero alocação de GC.",
            "context": "Projeto Unity 3D com código legado.",
            "action_spec": None
        }
    ]

    for s in scenarios:
        print(f"\n>>> {s['titulo']}")
        print(f"  Intenção do Desenvolvedor: '{s['intent']}'")
        print(f"  Contexto: {s['context']}")

        # 1. GATE 1 - Roteamento de Ferramenta
        route = gw.route_tool_call(s["intent"], s["context"])
        print(f"  [GATE 1 - ROTEAMENTO]: Ferramenta = '{route['selected_tool']}' | Confiança = {route['confidence']:.2f}")

        # Se for escalonamento, não tenta executar ação física
        if route["selected_tool"] == "escalate_to_reasoning":
            print("  -> Encaminhado para o modelo de raciocínio profundo (System Two).")
            continue

        # 2. GATE 2 - Guardrail de Segurança
        if s["action_spec"]:
            res = gw.execute_guarded_rcs_action(
                action_type=s["action_spec"]["type"],
                params=s["action_spec"]["params"],
                context_description=s["context"]
            )
            if res.get("blocked_by_guardrail"):
                print(f"  [GATE 2 - GUARDRAIL]: BLOQUEADO! {res['message']}")
            else:
                status_servidor = "OFFLINE (Aprovado em simulação)" if res.get("server_offline") else "EXECUTADO"
                print(f"  [GATE 2 - GUARDRAIL]: APROVADO! Prob. Segurança = {res['safety_probability']:.2f} | Status RCS: {status_servidor}")

    print("\n" + "=" * 70)
    print(" DEMONSTRAÇÃO CONCLUÍDA COM SUCESSO TOTAL!")
    print("=" * 70)

if __name__ == "__main__":
    main()
