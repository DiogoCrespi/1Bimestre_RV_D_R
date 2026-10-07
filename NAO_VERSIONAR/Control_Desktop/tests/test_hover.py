import urllib.request
import json
import time
import os

def focus_window(kw):
    req = urllib.request.Request('http://localhost:7842/window', data=json.dumps({"action":"focus", "window": kw}).encode('utf-8'), method='POST', headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        pass
    time.sleep(1)

def test_hover(target_window=None):
    if target_window:
        focus_window(target_window)
        
    url = 'http://localhost:7842/probe'
    data = json.dumps({"force": True, "conf_min": 0.0, "conf_max": 1.0}).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST', headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
        t1 = time.time()
        probed = res.get('probed', 0)
        results = res.get('results', [])
        interactive = [m for m in results if m.get('interactive')]
        print(f"Window: {target_window or 'Desktop'}")
        print(f"Time: {t1-t0:.2f}s")
        print(f"Probed marks: {probed}")
        print(f"Interactive marks: {len(interactive)}")
        if interactive:
            print(f"Interactive tags: {[m['tag'] for m in interactive]}")
        print("-" * 40)
    except Exception as e:
        print(f"Error on {target_window}: {e}")

if __name__ == '__main__':
    # Test on some open windows
    test_hover(None)
    test_hover('Chrome')
    test_hover('Unity')
    test_hover('File Explorer')
