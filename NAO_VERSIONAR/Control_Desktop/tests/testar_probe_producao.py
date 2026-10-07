import urllib.request
import json
import time

def testar():
    time.sleep(1.5)
    print("1. Testando /health...")
    with urllib.request.urlopen("http://127.0.0.1:7842/health", timeout=5) as r:
        print("Health OK:", r.read().decode())

    print("\n2. Executando POST /probe com force=True...")
    t0 = time.time()
    req = urllib.request.Request("http://127.0.0.1:7842/probe", 
                                 data=json.dumps({"force": True, "max_probes": 3, "settle_ms": 180}).encode("utf-8"), 
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            res = json.loads(r.read())
            print(f"[SUCESSO] /probe respondeu em {time.time()-t0:.2f}s:")
            print(json.dumps(res, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[ERRO] /probe falhou após {time.time()-t0:.2f}s: {e}")

if __name__ == "__main__":
    testar()
