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
    return difflib.SequenceMatcher(None, s1, s2).ratio() >= 0.80

def get_som_state():
    with urllib.request.urlopen(f"{SERVER_URL}/state?mode=som", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def medir_flicker(n_capturas=30, intervalo_s=0.15):
    print("=" * 80)
    print(f"MEDIÇÃO EMPÍRICA DA DISTRIBUIÇÃO DE FLICKER DO WINRT OCR ({n_capturas} CAPTURAS)")
    print("=" * 80)
    
    capturas = []
    print(f"1. Coletando {n_capturas} capturas consecutivas com tela estática (intervalo {intervalo_s*1000:.0f}ms)...")
    for i in range(n_capturas):
        t0 = time.perf_counter()
        st = get_som_state()
        dt = time.perf_counter() - t0
        marks = st.get("marks", [])
        # Filtrar apenas marcas textuais do OCR (ignorar ícones Canny para medir puramente WinRT)
        texto_marks = [m for m in marks if m.get("type") == "text"]
        capturas.append(texto_marks)
        print(f"  Frame {i+1:02d}/{n_capturas}: {len(texto_marks)} palavras detectadas ({dt*1000:.1f}ms)", flush=True)
        time.sleep(intervalo_s)
        
    print(f"\n2. Analisando estabilidade temporal e eventos de flicker...", flush=True)
    
    # Rastrear trilhas de texto ao longo dos frames
    trilhas = []
    
    for f_idx, marcas_frame in enumerate(capturas):
        matched_trilhas = set()
        for m in marcas_frame:
            mc = m["center"]
            mt = m.get("text", "").strip().lower()
            
            melhor_trilha = None
            menor_dist = 999
            for t_idx, t in enumerate(trilhas):
                if t_idx in matched_trilhas:
                    continue
                dist = _dist(mc, t["center"])
                if dist <= 4.0 and (_similar(mt, t["text"]) or mt == t["text"]):
                    if dist < menor_dist:
                        menor_dist = dist
                        melhor_trilha = t_idx
                        
            if melhor_trilha is not None:
                trilhas[melhor_trilha]["presenca"][f_idx] = True
                matched_trilhas.add(melhor_trilha)
            else:
                p = [False] * n_capturas
                p[f_idx] = True
                trilhas.append({
                    "text": mt,
                    "center": mc,
                    "presenca": p
                })
                matched_trilhas.add(len(trilhas) - 1)
                
    total_trilhas = len(trilhas)
    print(f"Total de trilhas de texto identificadas: {total_trilhas}")
    
    # Filtrar trilhas persistentes que apareceram pelo menos 3 vezes no total
    trilhas_validas = [t for t in trilhas if sum(t["presenca"]) >= 3]
    print(f"Trilhas persistentes (>=3 aparições): {len(trilhas_validas)}")
    
    # Analisar eventos de gap (ausência temporária)
    gaps = []
    trilhas_com_flicker = 0
    total_gaps_k = defaultdict(int)
    
    for t in trilhas_validas:
        p = t["presenca"]
        indices_presentes = [i for i, val in enumerate(p) if val]
        teve_flicker = False
        for a, b in zip(indices_presentes[:-1], indices_presentes[1:]):
            k = b - a - 1
            if k > 0:
                gaps.append(k)
                total_gaps_k[k] += 1
                teve_flicker = True
        if teve_flicker:
            trilhas_com_flicker += 1
            
    print("\n" + "=" * 80)
    print("RESULTADOS EMPÍRICOS DA DISTRIBUIÇÃO DO FLICKER DO WINRT")
    print("=" * 80)
    
    print(f"Total de entidades textuais analisadas: {len(trilhas_validas)}")
    print(f"Entidades que sofreram flicker: {trilhas_com_flicker} ({trilhas_com_flicker/len(trilhas_validas)*100:.2f}%)")
    print(f"Entidades 100% estáveis (sem nenhum flicker): {len(trilhas_validas) - trilhas_com_flicker} ({(len(trilhas_validas)-trilhas_com_flicker)/len(trilhas_validas)*100:.2f}%)")
    print(f"Total de eventos de gap/flicker observados: {len(gaps)}")
    
    if gaps:
        print("\nDISTRIBUIÇÃO DE DURAÇÃO DO FLICKER (k frames ausentes):")
        total_ev = len(gaps)
        acumulado = 0
        cdf = {}
        for k in sorted(total_gaps_k.keys()):
            count = total_gaps_k[k]
            acumulado += count
            pct = (count / total_ev) * 100
            pct_acum = (acumulado / total_ev) * 100
            cdf[k] = pct_acum
            print(f"  k = {k} frame(s): {count:3d} ocorrências ({pct:5.2f}%) | Acumulado: {pct_acum:6.2f}%")
            
        print("\nCOBERTURA DA QUARENTENA POR N ESCOLHIDO:")
        for n in [1, 2, 3]:
            cob = sum(total_gaps_k[k] for k in total_gaps_k if k <= n)
            pct_cob = (cob / total_ev) * 100
            print(f"  N = {n}: {pct_cob:6.2f}% de todos os flickers absorvidos sem churn de removed+added")
            
        ganho_n1_para_n2 = (sum(total_gaps_k[k] for k in total_gaps_k if k <= 2) - sum(total_gaps_k[k] for k in total_gaps_k if k <= 1)) / total_ev * 100
        ganho_n2_para_n3 = (sum(total_gaps_k[k] for k in total_gaps_k if k <= 3) - sum(total_gaps_k[k] for k in total_gaps_k if k <= 2)) / total_ev * 100
        print(f"\nANÁLISE MARGINAL:")
        print(f"  Ganho marginal de N=1 -> N=2: +{ganho_n1_para_n2:.2f}% de retenção")
        print(f"  Ganho marginal de N=2 -> N=3: +{ganho_n2_para_n3:.2f}% de retenção")
    else:
        print("\nNenhum evento de flicker observado! O OCR foi 100% estável nas capturas.")
        
    res_final = {
        "n_capturas": n_capturas,
        "intervalo_ms": intervalo_s * 1000,
        "total_trilhas_validas": len(trilhas_validas),
        "trilhas_com_flicker": trilhas_com_flicker,
        "taxa_trilhas_com_flicker": round(trilhas_com_flicker / len(trilhas_validas), 4) if trilhas_validas else 0,
        "total_gaps_observados": len(gaps),
        "histograma_k": dict(total_gaps_k),
        "cobertura": {
            "N_1": round(sum(total_gaps_k[k] for k in total_gaps_k if k <= 1) / len(gaps), 4) if gaps else 1.0,
            "N_2": round(sum(total_gaps_k[k] for k in total_gaps_k if k <= 2) / len(gaps), 4) if gaps else 1.0,
            "N_3": round(sum(total_gaps_k[k] for k in total_gaps_k if k <= 3) / len(gaps), 4) if gaps else 1.0
        }
    }
    
    with open("flicker_winrt_distribuicao_real.json", "w", encoding="utf-8") as f:
        json.dump(res_final, f, indent=2, ensure_ascii=False)
        
    print("\nRelatório salvo em flicker_winrt_distribuicao_real.json")
    return res_final

if __name__ == "__main__":
    medir_flicker(n_capturas=30, intervalo_s=0.10)
