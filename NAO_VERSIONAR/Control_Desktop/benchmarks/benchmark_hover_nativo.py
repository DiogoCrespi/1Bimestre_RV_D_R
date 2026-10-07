import time
import json
import threading
import tkinter as tk
from tkinter import ttk
import ctypes
from ctypes import wintypes, windll, byref
from PIL import ImageStat

from remote_control_server import (
    user32, gdi32, ensure_desktop_access, run_in_desktop_thread,
    capture_raw_pil_image, win32_mouse_move, _dhash_bits, _hamming,
    POINT, RECT, _signature_source_image
)

def _hover_fingerprint_test(monitor, box, pad=6):
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

def _hover_delta_test(antes, depois):
    bits = _hamming(antes[0], depois[0])
    cor = max(abs(a - b) for a, b in zip(antes[1], depois[1]))
    return bits, round(cor, 1)

def probe_alvo(center, bbox, settle_ms=220, min_delta=3, min_color=6.0, pad=6):
    cx, cy = center
    origem = POINT()
    user32.GetCursorPos(byref(origem))
    
    t0 = time.time()
    antes = _hover_fingerprint_test("1", bbox, pad=pad)
    
    win32_mouse_move(cx, cy)
    time.sleep(settle_ms / 1000.0)
    
    atual = POINT()
    user32.GetCursorPos(byref(atual))
    preso = (abs(atual.x - cx) > 4 or abs(atual.y - cy) > 4)
    
    depois = _hover_fingerprint_test("1", bbox, pad=pad)
    elapsed_ms = round((time.time() - t0) * 1000, 1)
    
    win32_mouse_move(origem.x, origem.y)
    
    final_p = POINT()
    user32.GetCursorPos(byref(final_p))
    restaurado = (abs(final_p.x - origem.x) <= 2 and abs(final_p.y - origem.y) <= 2)
    
    bits, cor = _hover_delta_test(antes, depois)
    interativo = (bits >= min_delta or cor >= min_color)
    
    return {
        "forma_bits": bits,
        "delta_cor": cor,
        "interativo": interativo,
        "elapsed_ms": elapsed_ms,
        "cursor_preso": preso,
        "cursor_restaurado": restaurado
    }

class HoverGroundTruthApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Hover GroundTruth Calibration")
        self.root.geometry("680x480+120+120")
        self.root.configure(bg="#1e1e1e")
        self.root.attributes("-topmost", True)
        
        self.tooltip_window = None
        self.anim_step = 0
        self.setup_ui()
        
    def setup_ui(self):
        header = tk.Label(self.root, text="Validação Científica de Hover Ping (Windows Real)", 
                          bg="#1e1e1e", fg="#61dafb", font=("Segoe UI", 12, "bold"))
        header.pack(pady=10)
        
        f = tk.Frame(self.root, bg="#1e1e1e")
        f.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # 1. Botão que muda só cor (flat, sem borda diferente)
        self.b_color = tk.Label(f, text="1. Salvar (Só Cor)", bg="#2563eb", fg="white", 
                                font=("Segoe UI", 10, "bold"), padx=15, pady=8)
        self.b_color.grid(row=0, column=0, padx=15, pady=15)
        self.b_color.bind("<Enter>", lambda e: self.b_color.config(bg="#1d4ed8"))
        self.b_color.bind("<Leave>", lambda e: self.b_color.config(bg="#2563eb"))
        
        # 2. Botão com Animação (Transição gradual de 200ms)
        self.b_anim = tk.Label(f, text="2. Processar (Animação)", bg="#059669", fg="white", 
                               font=("Segoe UI", 10, "bold"), padx=15, pady=8)
        self.b_anim.grid(row=0, column=1, padx=15, pady=15)
        self.b_anim.bind("<Enter>", self.start_anim)
        self.b_anim.bind("<Leave>", self.stop_anim)
        
        # 3. Botão com Tooltip (Surge janela flutuante)
        self.b_tip = tk.Label(f, text="3. Ajuda (Com Tooltip)", bg="#d97706", fg="white", 
                              font=("Segoe UI", 10, "bold"), padx=15, pady=8)
        self.b_tip.grid(row=0, column=2, padx=15, pady=15)
        self.b_tip.bind("<Enter>", self.show_tip)
        self.b_tip.bind("<Leave>", self.hide_tip)
        
        # 4. Falso Botão (ESTÁTICO - visual idêntico a botão, mas inerte)
        self.b_fake = tk.Label(f, text="4. Desabilitado (Estático)", bg="#4b5563", fg="#d1d5db", 
                               font=("Segoe UI", 10, "bold"), padx=15, pady=8)
        self.b_fake.grid(row=1, column=0, padx=15, pady=15)
        
        # 5. Texto Estático Puro
        self.b_text = tk.Label(f, text="5. Texto Puro (Versão 1.0.4)", bg="#1e1e1e", fg="#9ca3af", 
                               font=("Segoe UI", 10))
        self.b_text.grid(row=1, column=1, padx=15, pady=15)
        
        # 6. Botão Estilo Unity (Micro-contraste: #383838 -> #484848)
        self.b_unity = tk.Label(f, text="6. Inspector (Estilo Unity)", bg="#383838", fg="#cccccc", 
                                font=("Segoe UI", 10), padx=15, pady=8, relief="solid", bd=1)
        self.b_unity.grid(row=1, column=2, padx=15, pady=15)
        self.b_unity.bind("<Enter>", lambda e: self.b_unity.config(bg="#484848"))
        self.b_unity.bind("<Leave>", lambda e: self.b_unity.config(bg="#383838"))
        
        # 7. Botão com Cursor Preso / Captura de Mouse
        self.b_lock = tk.Label(f, text="7. Cursor Lock (ClipCursor)", bg="#dc2626", fg="white", 
                               font=("Segoe UI", 10, "bold"), padx=15, pady=8)
        self.b_lock.grid(row=2, column=1, padx=15, pady=15)
        self.b_lock.bind("<Enter>", self.on_lock_enter)
        self.b_lock.bind("<Leave>", self.on_lock_leave)

    def start_anim(self, event):
        self.anim_step = 0
        self._anim_tick()
        
    def _anim_tick(self):
        # 5 passos de 40ms = 200ms de transição
        colors = ["#059669", "#08a574", "#0ab480", "#0dc48c", "#10b981"]
        if self.anim_step < len(colors):
            self.b_anim.config(bg=colors[self.anim_step])
            self.anim_step += 1
            self.root.after(40, self._anim_tick)
            
    def stop_anim(self, event):
        self.anim_step = 99
        self.b_anim.config(bg="#059669")
        
    def show_tip(self, event):
        x = self.b_tip.winfo_rootx() + 20
        y = self.b_tip.winfo_rooty() + 45
        self.tooltip_window = tk.Toplevel(self.root)
        self.tooltip_window.wm_overrideredirect(True)
        self.tooltip_window.geometry(f"+{x}+{y}")
        lbl = tk.Label(self.tooltip_window, text="Dica Flutuante de Contexto", 
                       bg="#111111", fg="#ffffff", padx=8, pady=4, relief="solid", bd=1)
        lbl.pack()
        
    def hide_tip(self, event):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None
            
    def on_lock_enter(self, event):
        # Restringe o cursor a um retângulo de 10x10 px simulando trava de engine
        rx = self.b_lock.winfo_rootx() + 10
        ry = self.b_lock.winfo_rooty() + 10
        rc = RECT(rx, ry, rx + 15, ry + 15)
        user32.ClipCursor(byref(rc))
        
    def on_lock_leave(self, event):
        user32.ClipCursor(None)

    def get_alvos_coordenadas(self):
        self.root.update()
        alvos = [
            {"id": "cor_only", "nome": "1. Botão Só Cor", "w": self.b_color, "esperado": True},
            {"id": "anim", "nome": "2. Botão Animação 200ms", "w": self.b_anim, "esperado": True},
            {"id": "tooltip", "nome": "3. Botão com Tooltip", "w": self.b_tip, "esperado": True},
            {"id": "fake_static", "nome": "4. Falso Botão Estático", "w": self.b_fake, "esperado": False},
            {"id": "static_text", "nome": "5. Texto Estático Puro", "w": self.b_text, "esperado": False},
            {"id": "subtle_unity", "nome": "6. Botão Sutil Unity", "w": self.b_unity, "esperado": True},
            {"id": "cursor_lock", "nome": "7. Botão com Cursor Lock", "w": self.b_lock, "esperado": True},
        ]
        res = []
        for a in alvos:
            w = a["w"]
            x = w.winfo_rootx()
            y = w.winfo_rooty()
            width = w.winfo_width()
            height = w.winfo_height()
            res.append({
                "id": a["id"],
                "nome": a["nome"],
                "esperado": a["esperado"],
                "bbox": [x, y, width, height],
                "center": [x + width // 2, y + height // 2]
            })
        return res

def rodar_experimento():
    ensure_desktop_access()
    app = HoverGroundTruthApp()
    
    # Executar em thread secundária para permitir que o Tkinter processe eventos de UI
    resultados_finais = {}
    
    def worker():
        time.sleep(1.0) # Espera render inicial
        alvos = app.get_alvos_coordenadas()
        print(f"\n[OK] Janela ativa renderizada. {len(alvos)} alvos com gabarito exato localizados.")
        for a in alvos:
            print(f"  -> {a['nome']:26s} | Center: {a['center']} | BBox: {a['bbox']}")
            
        # 1. Medir impacto de settle_ms
        settle_tempos = [50, 100, 150, 220, 300]
        por_settle = {}
        
        print("\n" + "="*80)
        print("EXPERIMENTO 1: TAXA DE DETECÇÃO vs TEMPO DE ASSENTAMENTO (settle_ms)")
        print("="*80)
        
        for s in settle_tempos:
            print(f"\n--- SETTLE_MS = {s}ms ---")
            itens = []
            for a in alvos:
                if a["id"] == "cursor_lock":
                    continue # Teste de lock feito a parte
                res = probe_alvo(a["center"], a["bbox"], settle_ms=s, min_delta=3, min_color=6.0)
                acertou = (res["interativo"] == a["esperado"])
                res["nome"] = a["nome"]
                res["esperado"] = a["esperado"]
                res["acertou"] = acertou
                itens.append(res)
                st = "OK " if acertou else "ERR"
                print(f"  [{st}] {a['nome']:26s} | Esp: {str(a['esperado']):5s} | Det: {str(res['interativo']):5s} | "
                      f"ΔForma: {res['forma_bits']:2d} bits | ΔCor: {res['delta_cor']:4.1f} | "
                      f"Tempo: {res['elapsed_ms']:5.1f}ms | Restored: {res['cursor_restaurado']}")
            por_settle[f"{s}ms"] = itens
            
        # 2. Avaliação de Limiares em settle_ms = 220ms
        print("\n" + "="*80)
        print("EXPERIMENTO 2: CALIBRAÇÃO DE LIMIARES (FALSOS POSITIVOS vs RECALL)")
        print("="*80)
        
        grade_min_color = [2.0, 4.0, 6.0, 8.0, 12.0, 16.0]
        grade_min_delta = [1, 2, 3, 4]
        
        calibracoes = []
        base_amostras = por_settle["220ms"]
        
        for md in grade_min_delta:
            for mc in grade_min_color:
                tp, fp, tn, fn = 0, 0, 0, 0
                for r in base_amostras:
                    det = (r["forma_bits"] >= md or r["delta_cor"] >= mc)
                    esp = r["esperado"]
                    if esp and det: tp += 1
                    elif not esp and det: fp += 1
                    elif not esp and not det: tn += 1
                    elif esp and not det: fn += 1
                acc = (tp + tn) / len(base_amostras)
                prec = tp / (tp + fp) if (tp + fp) > 0 else 0
                rec = tp / (tp + fn) if (tp + fn) > 0 else 0
                calibracoes.append({
                    "min_delta": md, "min_color": mc,
                    "accuracy": round(acc * 100, 1),
                    "precision": round(prec * 100, 1),
                    "recall": round(rec * 100, 1),
                    "fp": fp, "fn": fn
                })
                
        # Exibir melhores configurações
        calibracoes.sort(key=lambda c: (-c["accuracy"], c["fp"], -c["recall"]))
        print("Top 5 configurações de limiares (ordenadas por Acurácia e menor Falso Positivo):")
        for c in calibracoes[:5]:
            print(f"  ΔForma >= {c['min_delta']} bits OU ΔCor >= {c['min_color']:4.1f} -> "
                  f"Acurácia: {c['accuracy']}% | Precisão: {c['precision']}% | Recall: {c['recall']}% | FP: {c['fp']} | FN: {c['fn']}")
                  
        # 3. Teste de Cursor Preso (SetCapture / ClipCursor)
        print("\n" + "="*80)
        print("EXPERIMENTO 3: CASO DE CURSOR PRESO / CAPTURADO")
        print("="*80)
        alvo_lock = next(a for a in alvos if a["id"] == "cursor_lock")
        res_lock = probe_alvo(alvo_lock["center"], alvo_lock["bbox"], settle_ms=150)
        print(f"  Sondagem em alvo com ClipCursor:")
        print(f"    Cursor preso detectado?: {res_lock['cursor_preso']} (esperado: True)")
        print(f"    Ponteiro restaurado após sondagem?: {res_lock['cursor_restaurado']}")
        print(f"    Tempo total: {res_lock['elapsed_ms']}ms")
        
        user32.ClipCursor(None) # Garantia extra de liberação
        
        resultados_finais["por_settle"] = por_settle
        resultados_finais["calibracoes"] = calibracoes
        resultados_finais["cursor_lock"] = res_lock
        
        with open("hover_benchmark_relatorio_final.json", "w", encoding="utf-8") as f:
            json.dump(resultados_finais, f, indent=2, ensure_ascii=False)
            
        print("\n[SUCESSO] Relatório completo exportado para 'hover_benchmark_relatorio_final.json'.")
        time.sleep(0.5)
        app.root.quit()
        
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    app.root.mainloop()

if __name__ == "__main__":
    run_in_desktop_thread(rodar_experimento)
