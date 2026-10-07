import requests
import os
from typing import Dict, Any, List, Optional

class RemoteControlClient:
    """Cliente HTTP para comunicação atômica com o remote_control_server.py."""
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or os.environ.get("RCS_URL", "http://127.0.0.1:8765")).rstrip("/")
        self.session = requests.Session()

    def is_alive(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    def list_windows(self) -> List[Dict[str, Any]]:
        try:
            r = self.session.get(f"{self.base_url}/windows", timeout=2.0)
            if r.status_code == 200:
                return r.json().get("windows", [])
        except Exception:
            pass
        return []

    def focus_window(self, keyword: str) -> bool:
        try:
            r = self.session.post(f"{self.base_url}/window/focus", json={"keyword": keyword}, timeout=2.0)
            return r.status_code == 200 and r.json().get("ok", False)
        except Exception:
            return False

    def click(self, x: int, y: int) -> bool:
        try:
            r = self.session.post(f"{self.base_url}/click", json={"x": x, "y": y}, timeout=2.0)
            return r.status_code == 200 and r.json().get("ok", False)
        except Exception:
            return False

    def type_text(self, text: str) -> bool:
        try:
            r = self.session.post(f"{self.base_url}/type", json={"text": text}, timeout=3.0)
            return r.status_code == 200 and r.json().get("ok", False)
        except Exception:
            return False

    def send_hotkey(self, keys: List[str]) -> bool:
        try:
            r = self.session.post(f"{self.base_url}/action/key_combination", json={"keys": keys}, timeout=2.0)
            return r.status_code == 200 and r.json().get("ok", False)
        except Exception:
            return False
