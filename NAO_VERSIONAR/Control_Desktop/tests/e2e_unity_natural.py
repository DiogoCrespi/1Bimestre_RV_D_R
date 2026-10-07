import time
import json
import urllib.request
import urllib.error
import pyautogui
from remote_control_server import (
    focus_window, menu_de_contexto_ativo, run_in_desktop_thread, ensure_desktop_access
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

def rodar_teste():
    ensure_desktop_access()
    print("=" * 80)
    print("INICIANDO TESTE E2E DE scope='context_menu' EM AMBIENTE WINDOWS REAL (UNITY 6.5)")
    print("=" * 80)
    
    # 1. Focar na Unity
    ok, win = focus_window(hwnd=67032)
    if not ok:
        ok, win = focus_window(process_name="Unity.exe")
    if not ok:
        print("[ERRO] Falha ao focar Unity.")
        return False
        
    unity_hwnd = win["hwnd"]
    print(f"[OK] Unity ativa: HWND {unity_hwnd}")
    
    # 2. Abrir menu de contexto na Unity
    print("\n1. Abrindo menu de contexto na Unity...")
    pyautogui.rightClick(200, 200)
    time.sleep(0.7)
    
    # Validar que o menu abriu
    menu = menu_de_contexto_ativo(unity_hwnd)
    if not menu:
        print("[ERRO] Menu de contexto #32768 não detectado!")
        pyautogui.press('escape')
        return False
    print(f"[OK] Menu #32768 detectado: HWND {menu['hwnd']} | Rect: {menu['left']},{menu['top']} {menu['width']}x{menu['height']}")
    
    # 3. Ler SoM e encontrar palavras ambíguas existentes dentro e fora do menu
    st, _ = get_json("/state?mode=som")
    mx, my, mw, mh = menu['left'], menu['top'], menu['width'], menu['height']
    
    dentro = {}
    fora = {}
    for m in st.get("marks", []):
        if m.get("type") == "text":
            txt = m.get("text", "").strip().casefold()
            cx, cy = m["center"]
            if len(txt) >= 3 and not any(c in txt for c in ["+", "-", ":", "/"]):
                if mx <= cx <= mx + mw and my <= cy <= my + mh:
                    dentro[txt] = (cx, cy)
                else:
                    fora[txt] = (cx, cy)
                    
    intersecao = list(set(dentro.keys()).intersection(set(fora.keys())))
    print(f"[OCR] Termos encontrados simultaneamente DENTRO e FORA do menu: {intersecao}")
    
    if not intersecao:
        print("[AVISO] Nenhuma interseção direta automática. Usando 'camera'...")
        termo_alvo = "camera"
    else:
        termo_alvo = intersecao[0]
        
    print(f"\n[ALVO SELECIONADO]: '{termo_alvo}' (existe no menu e fora do menu)")
    
    # =========================================================================
    # TESTE A: Sem escopo (DEVE FALHAR COM ambiguous_target 409)
    # =========================================================================
    print("\n--- TESTE A: click_text SEM escopo (Ambíguo) ---")
    res_sem, status_sem = post_json("/control", {
        "action": "click_text", 
        "text": termo_alvo
    })
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
    pyautogui.press('escape')
    time.sleep(0.3)
    pyautogui.press('escape')
    time.sleep(0.3)
    
    # Atualizar snapshot do servidor
    get_json("/state?mode=som")
    
    res_c, status_c = post_json("/control", {
        "action": "click_text", 
        "text": termo_alvo, 
        "scope": "context_menu"
    })
    print(f"Status HTTP retornado: {status_c}")
    print(f"Resposta: {json.dumps(res_c, ensure_ascii=False)}")
    
    passou_c = (status_c == 409 and res_c.get("error_code") == "scope_unavailable" and res_c.get("scope_reason") == "sem_menu_aberto")
    print(f"-> TESTE C PASSOU?: {passou_c} (esperado: 409 scope_unavailable/sem_menu_aberto)")
    
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
    run_in_desktop_thread(rodar_teste)
