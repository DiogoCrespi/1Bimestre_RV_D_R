import urllib.request
import json
import time

def testar():
    # 1. Agente chama /state
    t0 = time.time()
    with urllib.request.urlopen('http://127.0.0.1:7842/state?mode=som') as r:
        st = json.loads(r.read())
    print(f"1. /state concluído em {time.time()-t0:.2f}s com {len(st.get('marks', []))} marcas")

    # 2. Agente chama /probe logo em seguida
    t0 = time.time()
    req = urllib.request.Request('http://127.0.0.1:7842/probe', 
                                 data=json.dumps({'force': True, 'max_probes': 3, 'settle_ms': 180}).encode('utf-8'), 
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req) as r:
        pb = json.loads(r.read())
    print(f"2. /probe concluído em {time.time()-t0:.2f}s:")
    print(json.dumps(pb, indent=2, ensure_ascii=False))

if __name__ == '__main__':
    testar()
