import time
import json
import math
import difflib
import urllib.request
from collections import defaultdict

SERVER_URL = "http://127.0.0.1:7842"

def _dist(c1, c2):
    return math.hypot(c1[0] - c2[0], c1[1] - c2[1])

def _similar(s1, s2):
    if s1 == s2:
        return True
    return difflib.SequenceMatcher(None, s1, s2).ratio() >= 0.75

def get_som_state():
    with urllib.request.urlopen(f"{SERVER_URL}/state?mode=som", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def executar_benchmark_flicker(n_capturas=40, intervalo_s=0.10):
    print("=" * 80)
    print(f"BENCHMARK DE FLICKER COM FONTES PEQUENAS E UI DENSA ({n_capturas} CAPTURAS)")
    print("=" * 80)
    
    capturas = []
    print(f"Coletando {n_capturas} capturas consecutivas com tela estática...")
    for i in range(n_capturas):
        t0 = time.perf_counter()
        st = get_som_state()
        dt = time.perf_counter() - t0
        marks = st.get("marks", [])
        # Filtrar marcas de texto do OCR
        texto_marks = [m for m in marks if m.get("type") == "text"]
        capturas.append(texto_marks)
        n_small = sum(1 for m in texto_marks if m.get("bbox", [0,0,0,0])[3] <= 13)
        print(f"  Frame {i+1:02d}/{n_capturas}: {len(texto_marks)} palavras totais | {n_small} pequenas (h<=13px) ({dt*1000:.1f}ms)", flush=True)
        time.sleep(intervalo_s)
        
    print("\nProcessando trilhas temporais de cada palavra...", flush=True)
    
    # Rastrear trilhas
    # Cada trilha: {"text": str, "center": (x,y), "bbox": (x,y,w,h), "history": [mark or None]*n_capturas}
    trilhas = []
    
    for f_idx, marcas_frame in enumerate(capturas):
        matched = set()
        for m in marcas_frame:
            mc = m["center"]
            mt = m.get("text", "").strip().lower()
            
            melhor_t = None
            menor_d = 999
            for t_idx, t in enumerate(trilhas):
                if t_idx in matched:
                    continue
                d = _dist(mc, t["center"])
                # Bounding box próxima (<= 4px de tolerância) e texto compatível
                if d <= 4.0 and (_similar(mt, t["text"]) or mt == t["text"]):
                    if d < menor_d:
                        menor_d = d
                        melhor_t = t_idx
                        
            if melhor_t is not None:
                trilhas[melhor_t]["history"][f_idx] = m
                # Atualizar centro móvel suave
                trilhas[melhor_t]["center"] = mc
                matched.add(melhor_t)
            else:
                hist = [None] * n_capturas
                hist[f_idx] = m
                trilhas.append({
                    "text": mt,
                    "center": mc,
                    "bbox": m.get("bbox", [0,0,0,0]),
                    "history": hist
                })
                matched.add(len(trilhas) - 1)
                
    # Separar em universo global e universo de fontes pequenas (h <= 13px)
    trilhas_validas = [t for t in trilhas if sum(1 for x in t["history"] if x is not None) >= 3]
    trilhas_pequenas = [t for t in trilhas_validas if t["bbox"][3] <= 13]
    
    print(f"Total de trilhas textuais persistentes (>=3 frames): {len(trilhas_validas)}")
    print(f"Total de trilhas de fontes pequenas (h <= 13px): {len(trilhas_pequenas)}")
    
    # Função para analisar gaps/flicker em um conjunto de trilhas
    def analisar_conjunto(conjunto, nome):
        gaps = [] # lista de comprimentos de gap k
        trilhas_flicker = 0
        total_gaps_k = defaultdict(int)
        
        # Auditoria de impostores:
        # Verifica se enquanto uma marca estava em gap (ausente), alguma outra marca
        # diferente ocupou exatamente a mesma posição (centro dist <= 4px mas texto incompatível)
        impostores_detectados = 0
        
        for t in conjunto:
            hist = t["history"]
            indices_presentes = [i for i, x in enumerate(hist) if x is not None]
            teve_flicker = False
            for a, b in zip(indices_presentes[:-1], indices_presentes[1:]):
                k = b - a - 1
                if k > 0:
                    gaps.append(k)
                    total_gaps_k[k] += 1
                    teve_flicker = True
                    
                    # Checar impostores nos frames intermediários
                    for gap_f in range(a + 1, b):
                        frame_marks = capturas[gap_f]
                        for fm in frame_marks:
                            d = _dist(fm["center"], t["center"])
                            if d <= 4.0:
                                fmt = fm.get("text", "").strip().lower()
                                if not _similar(fmt, t["text"]):
                                    impostores_detectados += 1
                                    print(f"    [IMPOSTOR DETECTADO em frame {gap_f}]: era '{t['text']}', apareceu '{fmt}' no mesmo ponto")
                                    
            if teve_flicker:
                trilhas_flicker += 1
                
        total_ev = len(gaps)
        taxa_flicker_entidades = (trilhas_flicker / len(conjunto)) if conjunto else 0.0
        
        # Simulação de Churn com diferentes N:
        # Sem carência (N=0): cada gap gera 1 removed no frame a+1 e 1 added no frame b. Churn = 2 * total_ev.
        # Com carência N: se k <= N, o gap é totalmente absorvido! Churn = 0 para esse gap.
        # Se k > N, a carência expira no frame a + N + 1 gerando removed, e em b gera added. Churn = 2.
        churn_n0 = 2 * total_ev
        churn_por_n = {}
        absorvidos_por_n = {}
        for n in [1, 2, 3]:
            abs_n = sum(total_gaps_k[k] for k in total_gaps_k if k <= n)
            absorvidos_por_n[n] = abs_n
            nao_abs = total_ev - abs_n
            churn_n = 2 * nao_abs
            churn_por_n[n] = churn_n
            
        return {
            "nome": nome,
            "total_entidades": len(conjunto),
            "entidades_com_flicker": trilhas_flicker,
            "taxa_flicker": taxa_flicker_entidades,
            "total_eventos_gap": total_ev,
            "histograma_k": dict(total_gaps_k),
            "impostores_detectados": impostores_detectados,
            "churn_n0": churn_n0,
            "churn_por_n": churn_por_n,
            "absorvidos_por_n": absorvidos_por_n,
            "reducao_churn_pct": {
                f"N_{n}": round((churn_n0 - churn_por_n[n]) / max(1, churn_n0) * 100, 2) for n in [1, 2, 3]
            }
        }
        
    res_global = analisar_conjunto(trilhas_validas, "Todas as Marcas")
    res_pequenas = analisar_conjunto(trilhas_pequenas, "Fontes Pequenas (h <= 13px)")
    
    print("\n" + "=" * 80)
    print("RELATÓRIO COMPARATIVO: TODAS AS MARCAS vs FONTES PEQUENAS")
    print("=" * 80)
    for res in [res_global, res_pequenas]:
        print(f"\n[{res['nome']}]")
        print(f"  Entidades analisadas:       {res['total_entidades']}")
        print(f"  Entidades com flicker:      {res['entidades_com_flicker']} ({res['taxa_flicker']*100:.2f}%)")
        print(f"  Total de gaps observados:   {res['total_eventos_gap']}")
        print(f"  Distribuição de k (frames): {res['histograma_k']}")
        print(f"  Impostores detectados:      {res['impostores_detectados']}")
        print(f"  Churn de diff (N=0):        {res['churn_n0']} eventos (removed+added)")
        for n in [1, 2, 3]:
            print(f"    -> N = {n}: {res['absorvidos_por_n'][n]}/{res['total_eventos_gap']} absorvidos | Churn restante: {res['churn_por_n'][n]} | Redução de churn: {res['reducao_churn_pct'][f'N_{n}']}%")
            
    # Salvar em JSON
    resultado_completo = {
        "n_capturas": n_capturas,
        "intervalo_ms": intervalo_s * 1000,
        "global": res_global,
        "fontes_pequenas": res_pequenas
    }
    with open("benchmark_flicker_fontes_pequenas.json", "w", encoding="utf-8") as f:
        json.dump(resultado_completo, f, indent=2, ensure_ascii=False)
        
    print("\nResultado salvo em benchmark_flicker_fontes_pequenas.json")
    return resultado_completo

if __name__ == "__main__":
    executar_benchmark_flicker(n_capturas=40, intervalo_s=0.10)
