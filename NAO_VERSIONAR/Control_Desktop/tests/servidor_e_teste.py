import time
import json
import threading
import urllib.request
from remote_control_server import ThreadingHTTPServer, Handler, inspect_screen_som

def testar_fluxo_completo():
    port = 7843 # Porta de teste isolada
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    st_th = threading.Thread(target=server.serve_forever, daemon=True)
    st_th.start()
    time.sleep(0.5)
    print(f"[OK] Servidor de teste ouvindo em 127.0.0.1:{port}")
    
    # 1. Teste /health
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
        print("1. Health:", r.read().decode())
        
    # 2. Teste /move
    req = urllib.request.Request(f"http://127.0.0.1:{port}/move", 
                                 data=json.dumps({"x": 600, "y": 600}).encode("utf-8"), 
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=5) as r:
        print(f"2. Move em {time.time()-t0:.3f}s:", r.read().decode())
        
    # 3. Teste /probe sem marks (com include_candidates=True)
    print("3. Enviando /probe para sondar candidatos da tela...")
    req_probe = urllib.request.Request(f"http://127.0.0.1:{port}/probe", 
                                       data=json.dumps({"force": True, "max_probes": 3, "settle_ms": 180}).encode("utf-8"), 
                                       headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req_probe, timeout=25) as r:
            res = json.loads(r.read())
            print(f"[OK] Probe HTTP respondeu em {time.time()-t0:.2f}s!")
            print(json.dumps(res, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[FALHA] Probe HTTP falhou após {time.time()-t0:.2f}s: {e}")
        
    server.shutdown()

if __name__ == "__main__":
    from remote_control_server import run_in_desktop_thread
    run_in_desktop_thread(testar_fluxo_completo)
