import urllib.request
import json
import time

def run_probe():
    # 1. Fetch current marks
    req1 = urllib.request.Request('http://localhost:7842/state?mode=som')
    try:
        with urllib.request.urlopen(req1, timeout=30) as r:
            res1 = json.loads(r.read())
            marks = res1.get('marks', [])
    except Exception as e:
        print("Error fetching state:", e)
        return

    print(f"Fetched {len(marks)} marks.")
    if not marks:
        return
        
    # Pick a few valid marks to probe
    to_probe = marks[:10]
    
    url = 'http://localhost:7842/probe'
    data = json.dumps({"force": True, "marks": to_probe, "conf_min": 0.0, "conf_max": 1.0, "max_probes": 10}).encode('utf-8')
    req2 = urllib.request.Request(url, data=data, method='POST', headers={'Content-Type': 'application/json'})
    
    t0 = time.time()
    try:
        with urllib.request.urlopen(req2, timeout=30) as r:
            res2 = json.loads(r.read())
            t1 = time.time()
            probed = res2.get('probed', 0)
            results = res2.get('results', [])
            interactive = [m for m in results if m.get('interactive')]
            print(f"Time: {t1-t0:.2f}s")
            print(f"Probed marks: {probed}")
            print(f"Interactive marks: {len(interactive)}")
            if interactive:
                print(f"Interactive tags: {[m['tag'] for m in interactive]}")
    except Exception as e:
        print("Error on probe:", e)

run_probe()
