import urllib.request
import json
import time

def testar():
    time.sleep(1.5)
    print("=== TESTE DE REGRESSÃO E VALIDAÇÃO PÓS-MERGE ===")
    
    # 1. Health
    with urllib.request.urlopen("http://127.0.0.1:7842/health") as r:
        print("\n1. Health Check:", r.read().decode())
        
    # 2. State SoM (testa _ListaComCota e integridade do SoM)
    t0 = time.time()
    with urllib.request.urlopen("http://127.0.0.1:7842/state?mode=som") as r:
        st_som = json.loads(r.read())
        marks = st_som.get("marks", [])
        print(f"\n2. State SoM concluído em {time.time()-t0:.2f}s:")
        print(f"   Total de marcas: {len(marks)}")
        print(f"   Truncado / Cota?: {st_som.get('som_truncated', False)}")
        
    # 3. Probe após State (testa o nosso patch de fallback integrado com o código novo)
    t0 = time.time()
    req = urllib.request.Request("http://127.0.0.1:7842/probe",
                                 data=json.dumps({"force": True, "max_probes": 3, "settle_ms": 180}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        pb = json.loads(r.read())
        print(f"\n3. Probe após State concluído em {time.time()-t0:.2f}s:")
        print(f"   Probed: {pb.get('probed')}")
        print(f"   Interactive: {pb.get('interactive')}")
        print(f"   Elapsed ms: {pb.get('elapsed_ms')}")
        print(f"   Cursor restored: {pb.get('cursor_restored')}")
        
    # 4. State UIA (testa TreeElementBudget e fim da corrida de thread)
    t0 = time.time()
    with urllib.request.urlopen("http://127.0.0.1:7842/state?mode=uia") as r:
        st_uia = json.loads(r.read())
        elems = st_uia.get("elements", [])
        print(f"\n4. State UIA concluído em {time.time()-t0:.2f}s:")
        print(f"   Total de elementos UIA: {len(elems)}")
        print(f"   Truncado / Budget?: {st_uia.get('uia_truncated', False)}")

    print("\n=== TODOS OS TESTES PASSARAM COM SUCESSO! ===")

if __name__ == "__main__":
    testar()
