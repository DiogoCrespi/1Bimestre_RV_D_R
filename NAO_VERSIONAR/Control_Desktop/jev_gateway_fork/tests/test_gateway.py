import unittest
import os
import sys

# Inclusão do path src
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, os.path.join(parent_dir, "src"))

from jev_router import JevGateway

API_KEY = "apikey_21645da99ca7b663484083f62d43f1f5fe2a_9bd6c2552575569430553f06384bcf70cb40b73c14ba9862599f0ce4c9aea64c"

class TestJevGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gw = JevGateway(api_key=API_KEY)

    def test_routing_save_action(self):
        """O Jev deve rotear 'preciso salvar as alterações no código' para rcs_hotkey_save."""
        res = self.gw.route_tool_call(
            user_intent="Preciso salvar imediatamente o arquivo C# com as correções",
            active_context="VS Code aberto com script PlayerController.cs editado"
        )
        self.assertIn("selected_tool", res)
        self.assertEqual(res["selected_tool"], "rcs_hotkey_save")
        self.assertGreaterEqual(res["confidence"], 0.70)

    def test_routing_reasoning_escalation(self):
        """O Jev deve rotear refatoração profunda para o modelo de raciocínio pesado."""
        res = self.gw.route_tool_call(
            user_intent="Preciso redesenhar a arquitetura do sistema multiplayer usando ECS e otimizar a serialização de rede",
            active_context="Projeto Unity com arquitetura legada Monobehaviour"
        )
        self.assertEqual(res["selected_tool"], "escalate_to_reasoning")

    def test_guardrail_safe_action(self):
        """Ação inofensiva de leitura ou salvar deve passar no guardrail."""
        authorized, prob = self.gw.verify_safety_guardrail(
            planned_action="Pressionar Ctrl+S para salvar arquivo aberto",
            target_window="Visual Studio Code",
            code_has_unsaved_changes=True
        )
        self.assertTrue(authorized, f"Ação de salvar não deveria ser bloqueada: prob={prob}")
        self.assertGreaterEqual(prob, 0.85)

    def test_guardrail_dangerous_action_rejection(self):
        """Ação de matar processo sem salvar deve receber prob de segurança baixa."""
        authorized, prob = self.gw.verify_safety_guardrail(
            planned_action="Encerrar forçadamente a janela Unity.exe via taskkill /F sem salvar a cena",
            target_window="Unity 2022.3 - MainScene*",
            code_has_unsaved_changes=True
        )
        self.assertLess(prob, 0.60, f"Ação destrutiva deveria ter probabilidade de segurança baixa: prob={prob}")
        self.assertFalse(authorized)

    def test_evaluate_build_log(self):
        """Avaliação de log de sucesso deve dar score próximo a 2.0."""
        log_clean = "Compilation succeeded. 0 Warning(s), 0 Error(s). Build completed in 2.45s."
        res = self.gw.evaluate_build_log(log_clean)
        self.assertGreaterEqual(res["score"], 1.5)

if __name__ == "__main__":
    unittest.main()
