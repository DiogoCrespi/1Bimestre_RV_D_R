import os
import sys
import time
import subprocess
import json
from pathlib import Path

# Carregar variáveis do .env
env_file = Path(__file__).resolve().parent / ".env"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from jev_router import JevGateway

def run_rcs(*args):
    rcs_script = Path(__file__).resolve().parent / "remote_control_server.py"
    cmd = [sys.executable, str(rcs_script)] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.stdout.strip()

def main():
    print("=" * 65)
    print("EXECUTANDO PROCESSO COMPLETO: JEV GATEWAY + RCS FAST CONTROL")
    print("=" * 65)

    gateway = JevGateway()
    print("[1/5] Jev Gateway conectado com sucesso.")

    # 1. Roteamento de navegação via Jev
    intent_nav = "Navegar para a página inicial do YouTube no Google Chrome"
    route_nav = gateway.route_tool_call(
        user_intent=intent_nav,
        active_context="Navegador aberto em Nova Guia",
        available_tools={
            "navigate_url": "Focar barra de endereço e navegar para URL",
            "close_browser": "Fechar o navegador",
            "idle": "Aguardar"
        }
    )
    print(f"\n[2/5] Jev Route: {route_nav['selected_tool']} (Confiança: {route_nav['confidence']:.2f})")

    # 2. Executar navegação atômica via Win32 (Ctrl+L -> URL -> Enter)
    print(" -> Focando barra de endereço (Ctrl+L) e digitando https://www.youtube.com ...")
    run_rcs("--window", "Google Chrome", "--hotkey", "ctrl", "l")
    time.sleep(0.3)
    run_rcs("--window", "Google Chrome", "--type-text", "https://www.youtube.com", "--enter")
    
    # Aguardar carregamento da página do YouTube
    print(" -> Aguardando 5 segundos para renderização da grade de vídeos...")
    time.sleep(5)

    # 3. Guardrail do Jev para clique no vídeo
    intent_click = "Clicar no primeiro card de vídeo do feed principal do YouTube"
    authorized, prob_safe = gateway.verify_safety_guardrail(
        planned_action="Clicar com o botão esquerdo no primeiro thumbnail de vídeo do YouTube em tela cheia",
        target_window="YouTube - Google Chrome",
        code_has_unsaved_changes=False
    )
    print(f"\n[3/5] Jev Guardrail: Autorizado = {authorized} (Probabilidade Segura: {prob_safe:.4f})")

    if not authorized:
        print("[ABORTADO] Bloqueado pelo Jev Guardrail.")
        return

    # 4. Disparar clique relativo na posição do primeiro vídeo (coluna 1, linha 1)
    # No YouTube desktop, a grade começa por volta de rx=0.35, ry=0.38
    print("\n[4/5] Disparando clique físico Win32 atômico via RCS no primeiro vídeo...")
    out_click = run_rcs("--window", "Chrome", "--client-rx", "0.35", "--client-ry", "0.38")
    print(f" -> Retorno RCS:\n{out_click}")

    time.sleep(4)

    # 5. Outcome Verifier: Checar título da janela ativa
    print("\n[5/5] Inspecionando estado final da janela pós-reprodução...")
    out_win = run_rcs("--windows")
    try:
        # Extrair JSON da saída
        start = out_win.find("{")
        end = out_win.rfind("}") + 1
        data = json.loads(out_win[start:end])
        chrome_wins = [w for w in data.get("windows", []) if "chrome.exe" in w.get("process", "")]
        print(" -> Janelas do Chrome detectadas:")
        for w in chrome_wins:
            print(f"    - Title: {w['title']} | HWND: {w['hwnd']}")
    except Exception as e:
        print(f" -> Detalhes brutos das janelas:\n{out_win}")

    print("\n" + "=" * 65)
    print("CICLO COMPLETO CONCLUÍDO COM SUCESSO.")
    print("=" * 65)

if __name__ == "__main__":
    main()
