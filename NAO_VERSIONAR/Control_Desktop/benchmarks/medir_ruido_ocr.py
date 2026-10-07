import time
import math
import json
from collections import defaultdict
from remote_control_server import (
    capture_raw_pil_image, _ocr_candidates, run_in_desktop_thread,
    ensure_desktop_access
)

def medir_ruido_ocr_estatico(amostras=10, intervalo=0.15):
    ensure_desktop_access()
    print(f"=== MEDIÇÃO 1: RUÍDO DE 'MOVED' DO OCR COM TELA PARADA ===")
    print(f"Coletando {amostras} capturas consecutivas com intervalo de {int(intervalo*1000)}ms...")
    
    capturas = []
    for i in range(amostras):
        img = capture_raw_pil_image(monitor="1", draw_cursor=False)
        cands = _ocr_candidates(img, lang="pt-BR")
        capturas.append(cands)
        time.sleep(intervalo)
        
    print(f"[OK] {len(capturas)} capturas realizadas.")
    total_itens = [len(c) for c in capturas]
    print(f"Contagem de textos por frame: {total_itens} (min: {min(total_itens)}, max: {max(total_itens)})")
    
    # Rastrear textos persistentes ao longo das capturas consecutivas
    # Para cada par consecutivo (frame_i, frame_i+1), medir o deslocamento de elementos coincidentes
    deltas_centro = []
    deltas_max_borda = []
    deslocamentos_por_texto = defaultdict(list)
    desaparecimentos = 0
    aparecimentos = 0
    
    for i in range(len(capturas) - 1):
        f1 = capturas[i]
        f2 = capturas[i + 1]
        
        # Mapear f1 por texto e posição aproximada
        # Um texto é o mesmo se o texto for idêntico e a distância inicial for < 30px
        usados_f2 = set()
        for item1 in f1:
            t1 = item1["text"]
            b1 = item1["box"] # (min_x, min_y, max_x, max_y)
            c1 = ((b1[0] + b1[2]) / 2.0, (b1[1] + b1[3]) / 2.0)
            
            # Buscar melhor correspondência em f2
            melhor_match = None
            menor_dist = 999999.0
            idx_match = -1
            
            for idx2, item2 in enumerate(f2):
                if idx2 in usados_f2:
                    continue
                if item2["text"] == t1:
                    b2 = item2["box"]
                    c2 = ((b2[0] + b2[2]) / 2.0, (b2[1] + b2[3]) / 2.0)
                    dist = math.hypot(c2[0] - c1[0], c2[1] - c1[1])
                    if dist < menor_dist and dist < 35.0:
                        menor_dist = dist
                        melhor_match = item2
                        idx_match = idx2
                        
            if melhor_match:
                usados_f2.add(idx_match)
                b2 = melhor_match["box"]
                c2 = ((b2[0] + b2[2]) / 2.0, (b2[1] + b2[3]) / 2.0)
                
                # Deslocamento do centro
                d_center = math.hypot(c2[0] - c1[0], c2[1] - c1[1])
                # Deslocamento máximo entre as 4 bordas
                d_borda = max(abs(b2[0] - b1[0]), abs(b2[1] - b1[1]),
                              abs(b2[2] - b1[2]), abs(b2[3] - b1[3]))
                              
                deltas_centro.append(d_center)
                deltas_max_borda.append(d_borda)
                deslocamentos_por_texto[t1].append(d_center)
            else:
                desaparecimentos += 1
                
        aparecimentos += (len(f2) - len(usados_f2))

    # Análise Estatística Quantitativa
    total_comparacoes = len(deltas_centro)
    zero_jitter = sum(1 for d in deltas_centro if d == 0.0)
    ate_1px = sum(1 for d in deltas_centro if d <= 1.0)
    ate_2px = sum(1 for d in deltas_centro if d <= 2.0)
    ate_3px = sum(1 for d in deltas_centro if d <= 3.0)
    acima_3px = sum(1 for d in deltas_centro if d > 3.0)
    
    media_centro = sum(deltas_centro) / total_comparacoes if total_comparacoes else 0
    max_centro = max(deltas_centro) if deltas_centro else 0
    media_borda = sum(deltas_max_borda) / len(deltas_max_borda) if deltas_max_borda else 0
    max_borda = max(deltas_max_borda) if deltas_max_borda else 0
    
    print("\n" + "="*80)
    print("RELATÓRIO QUANTITATIVO DE JITTER (DESLOCAMENTO DE OCR EM TELA ESTÁTICA)")
    print("="*80)
    print(f"Total de pares de palavras rastreadas: {total_comparacoes}")
    print(f"  Deslocamento Exatamente 0.0 px : {zero_jitter:5d} ({zero_jitter/total_comparacoes*100:5.2f}%)")
    print(f"  Deslocamento <= 1.0 px         : {ate_1px:5d} ({ate_1px/total_comparacoes*100:5.2f}%)")
    print(f"  Deslocamento <= 2.0 px         : {ate_2px:5d} ({ate_2px/total_comparacoes*100:5.2f}%)")
    print(f"  Deslocamento <= 3.0 px         : {ate_3px:5d} ({ate_3px/total_comparacoes*100:5.2f}%)")
    print(f"  Deslocamento > 3.0 px          : {acima_3px:5d} ({acima_3px/total_comparacoes*100:5.2f}%)")
    print(f"\nMétricas do Centro:")
    print(f"  Média: {media_centro:.3f} px | Máximo: {max_centro:.3f} px")
    print(f"Métricas das Bordas (BBox [min_x, min_y, max_x, max_y]):")
    print(f"  Média: {media_borda:.3f} px | Máximo: {max_borda:.3f} px")
    print(f"Instabilidade de Reconhecimento (flicker de detecção de palavra):")
    print(f"  Desaparecimentos: {desaparecimentos} | Novos aparecimentos: {aparecimentos}")
    
    # Textos que mais flutuaram
    flutuantes = sorted([(t, max(v), sum(v)/len(v)) for t, v in deslocamentos_por_texto.items() if max(v) > 0],
                        key=lambda x: -x[1])
    if flutuantes:
        print(f"\nExemplos de textos com maior jitter:")
        for t, mx, med in flutuantes[:5]:
            print(f"  - '{t}': máx={mx:.2f}px, média={med:.2f}px")
            
    relatorio = {
        "total_pares": total_comparacoes,
        "zero_jitter_pct": round(zero_jitter / total_comparacoes * 100, 2),
        "ate_1px_pct": round(ate_1px / total_comparacoes * 100, 2),
        "ate_2px_pct": round(ate_2px / total_comparacoes * 100, 2),
        "ate_3px_pct": round(ate_3px / total_comparacoes * 100, 2),
        "acima_3px_pct": round(acima_3px / total_comparacoes * 100, 2),
        "media_centro_px": round(media_centro, 3),
        "max_centro_px": round(max_centro, 3),
        "media_borda_px": round(media_borda, 3),
        "max_borda_px": round(max_borda, 3),
        "recomendacao_snapshot_move_tolerance": 4 if max_centro >= 3 else 3
    }
    with open("ruido_ocr_relatorio.json", "w", encoding="utf-8") as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
        
    print(f"\n[RECOMENDAÇÃO TÉCNICA] SNAPSHOT_MOVE_TOLERANCE ideal = {relatorio['recomendacao_snapshot_move_tolerance']} px")
    return relatorio

if __name__ == "__main__":
    run_in_desktop_thread(medir_ruido_ocr_estatico)
