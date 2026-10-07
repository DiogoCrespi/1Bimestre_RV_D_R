import time
import json
import urllib.request
import urllib.error
import ctypes
from ctypes import wintypes, windll, byref
import subprocess
import os

from remote_control_server import (
    user32, kernel32, RECT, POINT, ensure_desktop_access,
    run_in_desktop_thread, focus_window, menu_de_contexto_ativo
)

SERVER_URL = "http://127.0.0.1:7842"

def post_json(endpoint, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{SERVER_URL}{endpoint}", data=data, 
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return json.loads(body), e.code
        except Exception:
            return {"ok": False, "raw_error": body}, e.code
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

def get_json(endpoint):
    with urllib.request.urlopen(f"{SERVER_URL}{endpoint}", timeout=15) as r:
        return json.loads(r.read().decode("utf-8")), r.status

def rodar_e2e():
    ensure_desktop_access()
    print("=" * 80)
    print("INICIANDO TESTE E2E DE scope='context_menu' EM WINDOWS REAL (INDEPENDENTE DE IDIOMA)")
    print("=" * 80)
    
    # 1. Abrir ou focar no Bloco de Notas (Notepad) como documento de teste
    print("\n1. Preparando janela do Bloco de Notas (Notepad)...")
    ok, win = focus_window(process_name="notepad.exe")
    if not ok or not win:
        subprocess.Popen(["cmd.exe", "/c", "start", "notepad.exe"])
        time.sleep(2.0)
        ok, win = focus_window(process_name="notepad.exe")
        
    if not ok or not win:
        print("[ERRO] Falha ao abrir Notepad.")
        return
        
    notepad_hwnd = win["hwnd"]
    print(f"[OK] Notepad ativo: HWND {notepad_hwnd} | Rect: {win['left']},{win['top']} {win['width']}x{win['height']}")
    
    # Limpar texto anterior e clicar na área de edição
    edit_cx = win["left"] + 200
    edit_cy = win["top"] + 200
    post_json("/control", {"action": "click", "x": edit_cx, "y": edit_cy})
    time.sleep(0.3)
    post_json("/control", {"action": "hotkey", "key": "ctrl+a"})
    time.sleep(0.1)
    post_json("/control", {"action": "key", "key": "backspace"})
    time.sleep(0.3)
    
    # 2. Abrir menu de contexto com botão direito
    print("\n2. Abrindo menu de contexto (clique direito)...")
    post_json("/control", {"action": "right_click", "x": edit_cx, "y": edit_cy})
    time.sleep(0.6)
    
    # 3. Detectar menu #32768 ativo via servidor
    menu_desc = menu_de_contexto_ativo(notepad_hwnd)
    if not menu_desc:
        print("[ERRO] menu_de_contexto_ativo retornou None!")
        return
    print(f"[OK] Menu de contexto detectado: HWND {menu_desc['hwnd']} | Rect: {menu_desc['left']},{menu_desc['top']} {menu_desc['width']}x{menu_desc['height']}")
    
    # 4. Capturar SoM e ler o primeiro texto do menu (Independente de idioma)
    st_res, _ = get_json("/state?mode=som")
    marks = st_res.get("marks", [])
    
    # Filtrar marcas que estão estritamente dentro da caixa do menu
    mx, my, mw, mh = menu_desc['left'], menu_desc['top'], menu_desc['width'], menu_desc['height']
    itens_menu = []
    for m in marks:
        if m.get("type") == "text":
            cx, cy = m["center"]
            if mx <= cx <= mx + mw and my <= cy <= my + mh:
                txt = m.get("text", "").strip()
                if len(txt) >= 3 and not txt.isdigit():
                    itens_menu.append(txt)
                    
    print(f"Itens do menu lidos por OCR ({len(itens_menu)} achados): {itens_menu}")
    if not itens_menu:
        print("[ERRO] Nenhum item textual lido do menu de contexto.")
        return
        
    termo_alvo = itens_menu[0]
    print(f"[ALVO SELECIONADO DINAMICAMENTE]: '{termo_alvo}'")
    
    # 5. Fechar menu com Escape
    post_json("/control", {"action": "key", "key": "escape"})
    time.sleep(0.4)
    
    # 6. Escrever o termo_alvo dentro do documento do Notepad
    print(f"\n3. Inserindo termo '{termo_alvo}' no documento para criar ambiguidade...")
    post_json("/control", {"action": "type", "text": f"Texto de teste contendo {termo_alvo} no documento.\n"})
    time.sleep(0.5)
    
    # 7. Abrir o menu de contexto novamente!
    # Agora o termo_alvo existe em DOIS lugares: no documento e no menu!
    print("4. Reabrindo menu de contexto. Agora o termo existe em DOIS lugares!")
    post_json("/control", {"action": "right_click", "x": edit_cx, "y": edit_cy})
    time.sleep(0.6)
    
    # Validar que o menu está aberto
    menu_desc2 = menu_de_contexto_ativo(notepad_hwnd)
    print(f"Menu reaberto?: {bool(menu_desc2)}")
    
    # =========================================================================
    # TESTE A: Sem escopo (DEVE FALHAR COM ambiguous_target 409)
    # =========================================================================
    print("\n--- TESTE A: click_text SEM escopo (Ambíguo) ---")
    res_sem, status_sem = post_json("/control", {"action": "click_text", "text": termo_alvo})
    print(f"Status HTTP retornado: {status_sem}")
    print(f"Resposta: {json.dumps(res_sem, ensure_ascii=False)}")
    
    passou_a = (status_sem == 409 and res_sem.get("error_code") == "ambiguous_target")
    print(f"-> TESTE A PASSOU?: {passou_a} (esperado: 409 ambiguous_target)")
    
    # =========================================================================
    # TESTE B: Com scope='context_menu' (DEVE ACERTAR O MENU E RETORNAR 200)
    # =========================================================================
    print("\n--- TESTE B: click_text COM scope='context_menu' ---")
    res_com, status_com = post_json("/control", {
        "action": "click_text", 
        "text": termo_alvo, 
        "scope": "context_menu"
    })
    print(f"Status HTTP retornado: {status_com}")
    print(f"Resposta: {json.dumps(res_com, ensure_ascii=False)}")
    
    passou_b = (status_com == 200 and res_com.get("ok") is True and res_com.get("scope_resolved") == "context_menu")
    print(f"-> TESTE B PASSOU?: {passou_b} (esperado: 200 ok com scope_resolved='context_menu')")
    
    time.sleep(0.6)
    
    # =========================================================================
    # TESTE C: scope='context_menu' SEM MENU ABERTO (DEVE RECUSAR COM scope_unavailable 409)
    # =========================================================================
    print("\n--- TESTE C: click_text COM scope='context_menu' mas com menu FECHADO ---")
    # Garante que o menu foi fechado
    post_json("/control", {"action": "key", "key": "escape"})
    time.sleep(0.3)
    
    res_c, status_c = post_json("/control", {
        "action": "click_text", 
        "text": termo_alvo, 
        "scope": "context_menu"
    })
    print(f"Status HTTP retornado: {status_c}")
    print(f"Resposta: {json.dumps(res_c, ensure_ascii=False)}")
    
    passou_c = (status_c == 409 and res_c.get("error_code") == "scope_unavailable" and res_c.get("scope_reason") == "sem_menu_aberto")
    print(f"-> TESTE C PASSOU?: {passou_c} (esperado: 409 scope_unavailable/sem_menu_aberto)")
    
    # Fechar Notepad sem salvar
    post_json("/control", {"action": "hotkey", "key": "alt+f4"})
    time.sleep(0.3)
    post_json("/control", {"action": "key", "key": "n"}) # Não salvar
    
    e2e_res = {
        "termo_alvo": termo_alvo,
        "teste_a_ambiguous": {"status": status_sem, "passou": passou_a, "detalhes": res_sem},
        "teste_b_context_menu": {"status": status_com, "passou": passou_b, "detalhes": res_com},
        "teste_c_scope_unavailable": {"status": status_c, "passou": passou_c, "detalhes": res_c},
        "todos_passaram": bool(passou_a and passou_b and passou_c)
    }
    with open("e2e_context_menu_resultado.json", "w", encoding="utf-8") as f:
        json.dump(e2e_res, f, indent=2, ensure_ascii=False)
        
    print("\n" + "=" * 80)
    print(f"RESULTADO FINAL DO TESTE E2E: {'100% SUCESSO' if e2e_res['todos_passaram'] else 'FALHA'}")
    print("=" * 80)
    return e2e_res

if __name__ == "__main__":
    run_in_desktop_thread(rodar_e2e)
