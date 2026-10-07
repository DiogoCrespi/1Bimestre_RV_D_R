import time
import json
from remote_control_server import (
    inspect_screen_som, hover_probe, run_in_desktop_thread,
    read_state_snapshot, publish_state_snapshot
)

def testar():
    print("=== TESTANDO HOVER PROBE APÓS O MERGE ===")
    
    # 1. Teste direto do inspect_screen_som com include_candidates=True
    print("\n1. Inspecionando tela com include_candidates=True...")
    marks, _ = inspect_screen_som(monitor="1", draw_badges=False, include_candidates=True)
    
    total = len(marks)
    texts = [m for m in marks if m.get("type") == "text"]
    icons = [m for m in marks if m.get("type") == "icon"]
    candidates = [m for m in marks if m.get("type") == "icon_candidate"]
    
    print(f"Total marcas: {total}")
    print(f"Textos: {len(texts)}")
    print(f"Ícones aceitos (>= 0.85): {len(icons)}")
    print(f"Ícones candidatos (< 0.85): {len(candidates)}")
    
    if candidates:
        print("Exemplo de candidato:", candidates[0])
        
    # 2. Executar hover_probe nas marcas geradas
    print("\n2. Executando hover_probe nativo...")
    t0 = time.time()
    res = hover_probe(marks, monitor="1", max_probes=4, settle_ms=180)
    t_elapsed = round((time.time() - t0) * 1000, 1)
    
    print(f"Resultado hover_probe em {t_elapsed}ms:")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    
    # 3. Teste do bug do snapshot existente (cenário comum em produção)
    print("\n3. Verificando comportamento com snapshot existente:")
    # Simula um /state comum que gravou snapshot sem candidatos
    marks_normais, _ = inspect_screen_som(monitor="1", draw_badges=False, include_candidates=False)
    publish_state_snapshot("som", "1", marks=marks_normais)
    
    snap = read_state_snapshot()
    tem_candidatos = any(m.get("type") == "icon_candidate" for m in snap.get("marks", []))
    print(f"Snapshot normal tem candidatos?: {tem_candidatos}")
    
    # Se o /probe usar o snapshot normal sem candidatos:
    res_snap = hover_probe(snap.get("marks", []), monitor="1", max_probes=4)
    print(f"Hover probe no snapshot normal sem include_candidates: probed={res_snap.get('probed')}, reason={res_snap.get('reason')}")

if __name__ == "__main__":
    run_in_desktop_thread(testar)
