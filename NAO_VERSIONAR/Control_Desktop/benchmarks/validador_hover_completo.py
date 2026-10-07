import time
import json
import ctypes
from ctypes import wintypes, windll, byref
from PIL import Image, ImageStat
import numpy as np

# Importar diretamente as funções do servidor para teste rigoroso e instrumentação
from remote_control_server import (
    user32, gdi32, ensure_desktop_access, run_in_desktop_thread,
    capture_raw_pil_image, get_monitor_geom, focus_window,
    win32_mouse_move, _dhash_bits, _hamming, POINT, RECT,
    _signature_source_image, HOVER_PAD
)

def _hover_fingerprint_custom(monitor, box, pad=6):
    image, _window, origin = _signature_source_image(monitor=monitor)
    x, y, w, h = box
    left = x - origin[0] - pad
    top = y - origin[1] - pad
    right = left + w + pad * 2
    bottom = top + h + pad * 2
    left = max(0, min(left, image.size[0] - 2))
    top = max(0, min(top, image.size[1] - 2))
    right = max(left + 2, min(right, image.size[0]))
    bottom = max(top + 2, min(bottom, image.size[1]))
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    media = ImageStat.Stat(crop).mean[:3]
    return _dhash_bits(crop), tuple(media)

def _hover_delta_custom(antes, depois):
    bits = _hamming(antes[0], depois[0])
    cor = max(abs(a - b) for a, b in zip(antes[1], depois[1]))
    return bits, round(cor, 1)

def probe_element(center, bbox, monitor="1", settle_ms=220, pad=6, min_delta=3, min_color=6.0):
    cx, cy = center
    origem = POINT()
    user32.GetCursorPos(byref(origem))
    
    t0 = time.time()
    antes = _hover_fingerprint_custom(monitor, bbox, pad=pad)
    
    # Mover mouse
    win32_mouse_move(cx, cy)
    time.sleep(settle_ms / 1000.0)
    
    # Verificar se cursor chegou
    atual = POINT()
    user32.GetCursorPos(byref(atual))
    cursor_preso = (abs(atual.x - cx) > 4 or abs(atual.y - cy) > 4)
    
    depois = _hover_fingerprint_custom(monitor, bbox, pad=pad)
    t_probe = round((time.time() - t0) * 1000, 1)
    
    # Restaurar cursor
    win32_mouse_move(origem.x, origem.y)
    
    final_pos = POINT()
    user32.GetCursorPos(byref(final_pos))
    cursor_restaurado = (abs(final_pos.x - origem.x) <= 2 and abs(final_pos.y - origem.y) <= 2)
    
    bits, cor = _hover_delta_custom(antes, depois)
    interativo = (bits >= min_delta or cor >= min_color)
    
    return {
        "bits": bits,
        "cor": cor,
        "interativo": interativo,
        "elapsed_ms": t_probe,
        "cursor_preso": cursor_preso,
        "cursor_restaurado": cursor_restaurado,
        "origem": (origem.x, origem.y),
        "final": (final_pos.x, final_pos.y)
    }

def executar_bateria():
    ensure_desktop_access()
    print("=== INICIANDO BATERIA DE VALIDAÇÃO DO HOVER PING EM AMBIENTE REAL ===")
    
    # 1. Localizar janela do Chrome com a página de teste
    ok, win = focus_window(title_kw="Hover Ping Validation Suite")
    if not ok or not win:
        print("[AVISO] Janela 'Hover Ping Validation Suite' não encontrada por título. Tentando 'Google Chrome'...")
        ok, win = focus_window(process_name="chrome.exe")
    
    if not win:
        print("[ERRO] Chrome não encontrado!")
        return
        
    print(f"[OK] Focado na janela: HWND {win['hwnd']} | {win['title'][:50]} | Rect: {win['left']},{win['top']} {win['width']}x{win['height']}")
    time.sleep(1.0)
    
    # Tirar screenshot para OCR e localização dos cartões
    from remote_control_server import inspect_screen_som
    marks, _ = inspect_screen_som(monitor="1", draw_badges=False)
    
    print(f"Total de marcas OCR/SoM encontradas: {len(marks)}")
    
    # Encontrar os alvos pelos textos conhecidos na página
    casos_teste = [
        {"nome": "1. Só Cor (Salvar Dados)", "texto": "Salvar Dados", "esperado": True, "tipo": "cor_only"},
        {"nome": "2. Animação 250ms (Processar)", "texto": "Processar", "esperado": True, "tipo": "anim"},
        {"nome": "3. Tooltip (Ajuda Info)", "texto": "Ajuda Info", "esperado": True, "tipo": "tooltip"},
        {"nome": "4. Falso Botão (Desabilitado)", "texto": "Desabilitado", "esperado": False, "tipo": "fake_static"},
        {"nome": "5. Texto Puro (Versão 1.0.4)", "texto": "Versão 1.0.4", "esperado": False, "tipo": "static_text"},
        {"nome": "6. Hover Sutil Unity (Inspector Tool)", "texto": "Inspector Tool", "esperado": True, "tipo": "subtle_unity"},
    ]
    
    alvos_localizados = []
    for c in casos_teste:
        achou = None
        for m in marks:
            t = str(m.get("text", "")).strip().lower()
            if c["texto"].lower() in t:
                achou = m
                break
        if achou:
            c["mark"] = achou
            alvos_localizados.append(c)
            print(f"  [LOCALIZADO] {c['nome']} -> bbox: {achou['bbox']}, center: {achou['center']}")
        else:
            print(f"  [NÃO ACHADO] {c['nome']}")
            
    # Adicionar alvos do Windows nativo
    # Botão Fechar da janela do Chrome (canto superior direito)
    alvos_localizados.append({
        "nome": "7. Botão Fechar do Chrome (Win32/DWM)",
        "texto": "[Close Button]",
        "esperado": True,
        "tipo": "win32_close_btn",
        "mark": {
            "bbox": [win["left"] + win["width"] - 46, win["top"] + 2, 45, 30],
            "center": [win["left"] + win["width"] - 23, win["top"] + 15]
        }
    })
    
    # Barra de Tarefas (ícone do Windows / Iniciar no canto inferior esquerdo)
    alvos_localizados.append({
        "nome": "8. Menu Iniciar do Windows (Taskbar)",
        "texto": "[Start Button]",
        "esperado": True,
        "tipo": "win32_start_btn",
        "mark": {
            "bbox": [10, 830, 36, 32],
            "center": [28, 846]
        }
    })

    # Barra de Tarefas espaço vazio (Estático)
    alvos_localizados.append({
        "nome": "9. Barra de Tarefas Espaço Vazio (Estático)",
        "texto": "[Taskbar Empty]",
        "esperado": False,
        "tipo": "taskbar_empty",
        "mark": {
            "bbox": [800, 830, 50, 32],
            "center": [825, 846]
        }
    })

    print(f"\nTotal de alvos prontos para validação: {len(alvos_localizados)}")
    
    # Rodar testes em diferentes condições de settle_ms (100ms, 220ms, 350ms)
    resultados_globais = {}
    
    for settle in [100, 220, 350]:
        print(f"\n--- TESTANDO COM SETTLE_MS = {settle}ms ---")
        res_settle = []
        for alvo in alvos_localizados:
            m = alvo["mark"]
            res = probe_element(m["center"], m["bbox"], settle_ms=settle, min_delta=3, min_color=6.0)
            correto = (res["interativo"] == alvo["esperado"])
            res_settle.append({
                "alvo": alvo["nome"],
                "esperado": alvo["esperado"],
                "detectado": res["interativo"],
                "acertou": correto,
                "forma_bits": res["bits"],
                "delta_cor": res["cor"],
                "elapsed_ms": res["elapsed_ms"],
                "cursor_ok": res["cursor_restaurado"]
            })
            status = "ACERTO" if correto else "ERRO"
            print(f"  [{status:6s}] {alvo['nome']:42s} | Esp: {str(alvo['esperado']):5s} | Det: {str(res['interativo']):5s} | ΔForma: {res['bits']:2d} | ΔCor: {res['cor']:4.1f} | {res['elapsed_ms']}ms | CursorRestored: {res['cursor_restaurado']}")
        resultados_globais[f"{settle}ms"] = res_settle

    # Teste de Estresse de Limiares (Variação de min_delta e min_color com 220ms)
    print(f"\n--- CALIBRAÇÃO DE LIMIARES (settle_ms=220ms) ---")
    grade_limiares = [
        {"min_delta": 2, "min_color": 4.0},
        {"min_delta": 3, "min_color": 6.0}, # Padrão
        {"min_delta": 4, "min_color": 10.0},
    ]
    
    calib_resultados = []
    for cfg in grade_limiares:
        md = cfg["min_delta"]
        mc = cfg["min_color"]
        tp = 0 # Verdadeiro Positivo
        fp = 0 # Falso Positivo
        tn = 0 # Verdadeiro Negativo
        fn = 0 # Falso Negativo
        for r in resultados_globais["220ms"]:
            det = (r["forma_bits"] >= md or r["delta_cor"] >= mc)
            esp = r["esperado"]
            if esp and det: tp += 1
            elif not esp and det: fp += 1
            elif not esp and not det: tn += 1
            elif esp and not det: fn += 1
        
        acc = (tp + tn) / len(resultados_globais["220ms"])
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        calib_resultados.append({
            "min_delta": md, "min_color": mc,
            "acuracia": round(acc * 100, 1),
            "precisao": round(prec * 100, 1),
            "recall": round(rec * 100, 1),
            "fp": fp, "fn": fn
        })
        print(f"  Limiares (bits>={md}, cor>={mc:4.1f}) -> Acurácia: {acc*100:5.1f}% | Precisão: {prec*100:5.1f}% | Recall: {rec*100:5.1f}% | FP: {fp} | FN: {fn}")

    # Teste de Cursor Preso (Simulação Win32 SetCapture)
    print("\n--- TESTE DE CURSOR PRESO (SetCapture / ClipCursor) ---")
    # Vamos criar uma janela temporária com ClipCursor restringindo a um retângulo de 10x10
    clip_rc = RECT(100, 100, 110, 110)
    user32.ClipCursor(byref(clip_rc))
    time.sleep(0.05)
    
    # Tentar sondar alvo fora do clip
    m_teste = alvos_localizados[0]["mark"]
    res_preso = probe_element(m_teste["center"], m_teste["bbox"], settle_ms=100)
    
    # Liberar ClipCursor imediatamente
    user32.ClipCursor(None)
    
    print(f"  Detecção de Cursor Preso funcionou?: {res_preso['cursor_preso']} (esperado: True)")
    print(f"  Cursor foi restaurado após liberação?: {res_preso['cursor_restaurado']}")
    
    relatorio = {
        "resultados_por_settle": resultados_globais,
        "calibracao_limiares": calib_resultados,
        "cursor_preso_detectado": res_preso["cursor_preso"],
        "cursor_restaurado_preso": res_preso["cursor_restaurado"]
    }
    with open("resultado_hover_benchmark.json", "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
    print("\n[OK] Benchmark completo gravado em 'resultado_hover_benchmark.json'.")

if __name__ == "__main__":
    run_in_desktop_thread(executar_bateria)
