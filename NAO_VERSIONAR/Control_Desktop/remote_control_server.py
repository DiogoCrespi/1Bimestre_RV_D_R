import io
import os
import threading
import time
import subprocess
import sys
import ctypes
from ctypes import windll, wintypes, byref, c_void_p
import json
import argparse
import uuid
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import base64
import ast
import atexit
import collections
import hashlib
import math
import re
import queue
import logging

# Configurar encoding UTF-8 no console Windows para evitar quedas por caracteres Unicode/emojis
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- Log estruturado (timestamp + nível) ---
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rcs")

# --- Rate limiting: max 12 requests simultâneas no ThreadingHTTPServer ---
# Sem isso um loop de agente descontrolado pode abrir 200+ threads e travar.
_http_semaphore = threading.Semaphore(12)

# --- Cache de HWND com TTL de 2s ---
# find_window_by_keyword percorre todas as janelas a cada chamada; com Unity +
# ShaderCompilers abertos isso é custoso. O cache evita EnumWindows repetidos
# para o mesmo keyword dentro da janela de TTL.
_hwnd_cache: dict = {}          # keyword -> (hwnd, win_dict, expires_at)
_hwnd_cache_lock = threading.Lock()
HWND_CACHE_TTL = 2.0            # segundos

def _hwnd_cache_get(key: str):
    with _hwnd_cache_lock:
        entry = _hwnd_cache.get(key)
        if entry and time.monotonic() < entry[2]:
            return entry[0], entry[1]
    return None, None

def _hwnd_cache_set(key: str, hwnd, win_dict):
    with _hwnd_cache_lock:
        _hwnd_cache[key] = (hwnd, win_dict, time.monotonic() + HWND_CACHE_TTL)

def _hwnd_cache_invalidate(key: str = None):
    with _hwnd_cache_lock:
        if key:
            _hwnd_cache.pop(key, None)
        else:
            _hwnd_cache.clear()

# --- Fila de ações de teclado com worker dedicado ---
# O input_action_lock bloqueia a thread HTTP inteira durante digitação longa.
# A fila desacopla: POST retorna imediatamente; o worker serializa as ações.
_kb_queue: queue.Queue = queue.Queue()
KB_JOB_TIMEOUT = max(1.0, float(os.environ.get("KB_JOB_TIMEOUT", "30")))


class _KbJob:
    """Uma ação de teclado enfileirada, com seu próprio evento de conclusão."""
    __slots__ = ("fn", "args", "kwargs", "done", "result", "abandoned")

    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.done = threading.Event()
        self.result = None
        self.abandoned = False


def _keyboard_worker():
    while True:
        job = _kb_queue.get()
        try:
            if job is None:
                return
            if job.abandoned:
                # O cliente ja desistiu. Executar agora mandaria a tecla para
                # qualquer janela que estiver em foco daqui a minutos.
                job.result = {"ok": False, "error_code": "keyboard_timeout",
                              "error": "acao descartada: o cliente desistiu antes do worker chegar nela"}
            else:
                job.result = job.fn(*job.args, **job.kwargs)
        except Exception as exc:
            job.result = {"ok": False, "error": str(exc)}
        finally:
            if job is not None:
                job.done.set()
            _kb_queue.task_done()


_kb_worker_thread = None
_kb_worker_lock = threading.Lock()


def ensure_kb_worker():
    """Sobe o worker de teclado, e o ressuscita caso a thread tenha morrido."""
    global _kb_worker_thread
    with _kb_worker_lock:
        if _kb_worker_thread is None or not _kb_worker_thread.is_alive():
            _kb_worker_thread = threading.Thread(
                target=_keyboard_worker, daemon=True, name="kb-worker")
            _kb_worker_thread.start()
    return _kb_worker_thread


def kb_dispatch(fn, *args, **kwargs):
    """Serializa a ação de teclado no worker dedicado e espera o resultado.

    A espera e por evento, nao por poll: o antigo laco dormia 10 ms ANTES de
    olhar o resultado, entao toda tecla pagava 10 ms de piso mesmo quando o
    worker terminava em microssegundos. O resultado tambem vive no proprio job,
    entao um timeout nao deixa mais entrada orfa em dicionario global.
    """
    ensure_kb_worker()
    _marcar_entrada_enviada()
    job = _KbJob(fn, args, kwargs)
    _kb_queue.put(job)
    if job.done.wait(KB_JOB_TIMEOUT):
        return job.result
    job.abandoned = True
    return {"ok": False, "error_code": "keyboard_timeout",
            "error": f"acao de teclado nao concluiu em {KB_JOB_TIMEOUT:g}s"}



def load_local_env(path=None):
    """Carrega variáveis simples do .env sem sobrescrever o ambiente do Windows."""
    env_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8-sig") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip().strip('"').strip("'")
                if name:
                    os.environ.setdefault(name, value)
    except OSError as exc:
        print(f"[AVISO] Não foi possível carregar {env_path}: {exc}", file=sys.stderr)

load_local_env()

# --- IDXGI Invalidation Oracle (Opt-in) ---
# Quando ativado via CLI (--enable-dxgi-oracle) ou env (ENABLE_DXGI_ORACLE=1),
# atua como oráculo de detecção de 'mudou / não mudou' para evitar OCR e GDI desnecessários.
# Padrão: FALSE (desativado, mantendo pipeline síncrono padrão inalterado).
ENABLE_DXGI_ORACLE = os.environ.get("ENABLE_DXGI_ORACLE", "0").strip().lower() in ("1", "true", "yes")
_dxgi_oracle_instance = None
_dxgi_oracle_lock = threading.Lock()

def get_dxgi_oracle():
    global _dxgi_oracle_instance
    if not ENABLE_DXGI_ORACLE:
        return None
    with _dxgi_oracle_lock:
        if _dxgi_oracle_instance is None:
            try:
                from testes_idxgi.dxgi_snapshot_service import DXGISnapshotService
                _dxgi_oracle_instance = DXGISnapshotService.get_instance(enabled=True)
                _dxgi_oracle_instance.start()
                log.info("DXGI Invalidation Oracle ativo e iniciado (opt-in).")
            except Exception as exc:
                log.warning("Falha ao inicializar DXGISnapshotService: %s", exc)
                return None
        return _dxgi_oracle_instance

# Declarar DPI Awareness para evitar virtualização e deslocamento de coordenadas no Windows
try:
    windll.shcore.SetProcessDpiAwareness(2) # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# Auto-instala dependências essenciais
def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

try:
    from PIL import Image, ImageGrab, ImageDraw, ImageFont, ImageStat
except ImportError:
    install("pillow")
    from PIL import Image, ImageGrab, ImageDraw, ImageFont, ImageStat

try:
    import pyautogui
except ImportError:
    install("pyautogui")
    import pyautogui

try:
    import pyperclip
except ImportError:
    install("pyperclip")
    import pyperclip

try:
    import uiautomation as uia
except ImportError:
    try:
        install("uiautomation")
        import uiautomation as uia
    except Exception:
        uia = None

try:
    import winocr
except ImportError:
    try:
        install("winocr")
        import winocr
    except Exception:
        winocr = None

try:
    import cv2
    import numpy as np
except ImportError:
    try:
        install("opencv-python-headless")
        install("numpy")
        import cv2
        import numpy as np
    except Exception:
        cv2 = None
        np = None

pyautogui.FAILSAFE = False

pyautogui.PAUSE = 0

# Patch de estabilidade para caracteres com Shift (corrige bug do PyAutoGUI em layout de teclado)
_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:"<>?'
try:
    pyautogui.isShiftCharacter = lambda character: character.isupper() or character in _shift_chars
except Exception:
    pass

class RECT(ctypes.Structure):
    _fields_ = [
        ('left', ctypes.c_long),
        ('top', ctypes.c_long),
        ('right', ctypes.c_long),
        ('bottom', ctypes.c_long)
    ]

class POINT(ctypes.Structure):
    _fields_ = [
        ('x', ctypes.c_long),
        ('y', ctypes.c_long)
    ]

class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('flags', wintypes.DWORD),
        ('hCursor', wintypes.HICON),
        ('ptScreenPos', POINT)
    ]

class ICONINFO(ctypes.Structure):
    _fields_ = [
        ('fIcon', wintypes.BOOL),
        ('xHotspot', wintypes.DWORD),
        ('yHotspot', wintypes.DWORD),
        ('hbmMask', wintypes.HBITMAP),
        ('hbmColor', wintypes.HBITMAP),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD)
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER)]

# Estruturas de baixo nível para injeção atômica via Win32 SendInput (Kernel Input Queue)
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ('dx', wintypes.LONG),
        ('dy', wintypes.LONG),
        ('mouseData', wintypes.DWORD),
        ('dwFlags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ULONG_PTR)
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk', wintypes.WORD),
        ('wScan', wintypes.WORD),
        ('dwFlags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ULONG_PTR)
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ('uMsg', wintypes.DWORD),
        ('wParamL', wintypes.WORD),
        ('wParamH', wintypes.WORD)
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ('mi', MOUSEINPUT),
        ('ki', KEYBDINPUT),
        ('hi', HARDWAREINPUT)
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ('type', wintypes.DWORD),
        ('u', INPUT_UNION)
    ]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

user32 = windll.user32
gdi32 = windll.gdi32

# Conta cada lote de entrada que sai de verdade para o sistema. Serve para
# responder "isso chegou a ser enviado?" MEDINDO, em vez de inferir pela forma
# do resultado - a diferenca entre o agente poder repetir com seguranca e
# clicar duas vezes. E global e nao por thread porque o teclado sai numa thread
# worker (kb_dispatch) diferente da que atende o HTTP. Acao concorrente de
# outra thread pode inflar a contagem, o que erra para o lado seguro: "talvez
# tenha saido" nunca vira "nao saiu".
_entrada_enviada = 0


def _marcar_entrada_enviada():
    global _entrada_enviada
    _entrada_enviada += 1

# Tipagem estrita de 64-bit para evitar overflows de ponteiros e falhas de handles
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]

user32.CloseDesktop.restype = wintypes.BOOL
user32.CloseDesktop.argtypes = [wintypes.HANDLE]

user32.GetDC.restype = c_void_p
user32.GetDC.argtypes = [c_void_p]

user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [c_void_p, c_void_p]

gdi32.CreateCompatibleDC.restype = c_void_p
gdi32.CreateCompatibleDC.argtypes = [c_void_p]

gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [c_void_p]

gdi32.CreateDIBSection.restype = c_void_p
gdi32.CreateDIBSection.argtypes = [c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(c_void_p), c_void_p, wintypes.DWORD]

gdi32.SelectObject.restype = c_void_p
gdi32.SelectObject.argtypes = [c_void_p, c_void_p]

gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [c_void_p]

gdi32.SetStretchBltMode.restype = ctypes.c_int
gdi32.SetStretchBltMode.argtypes = [c_void_p, ctypes.c_int]

gdi32.StretchBlt.restype = wintypes.BOOL
gdi32.StretchBlt.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]

user32.GetCursorInfo.restype = wintypes.BOOL
user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]

user32.GetIconInfo.restype = wintypes.BOOL
user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]

user32.DrawIconEx.restype = wintypes.BOOL
user32.DrawIconEx.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int, ctypes.c_int, wintypes.UINT, c_void_p, wintypes.UINT]

MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, c_void_p, c_void_p, ctypes.POINTER(RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.EnumDisplayMonitors.argtypes = [c_void_p, c_void_p, MONITORENUMPROC, wintypes.LPARAM]

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumThreadWindows.restype = wintypes.BOOL
user32.EnumThreadWindows.argtypes = [wintypes.DWORD, WNDENUMPROC, wintypes.LPARAM]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.MonitorFromPoint.restype = c_void_p
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

user32.ClientToScreen.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]

user32.ScreenToClient.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]

user32.IsIconic.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]

user32.GetClientRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]

user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [c_void_p]

user32.SetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

user32.ClipCursor.restype = wintypes.BOOL
user32.ClipCursor.argtypes = [c_void_p]

user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]

kernel32 = windll.kernel32

# Sem restype explicito o ctypes assume c_int (32-bit) e trunca handles/HWND em
# processos 64-bit. O resto do arquivo ja declarava tudo; faltavam estes.
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []

user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]

user32.BringWindowToTop.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]

user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

user32.AllowSetForegroundWindow.restype = wintypes.BOOL
user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]

user32.AttachThreadInput.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]

user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

user32.keybd_event.restype = None
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, c_void_p]

kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = []

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

MONITOR_DEFAULTTONULL = 0


class CoordenadaForaDaTela(ValueError):
    """Coordenada pedida que não cai em pixel nenhum de monitor.

    Antes ela era presa na borda pela normalizacao e o clique acontecia la,
    com ok=True: x=99999 virava clique na borda direita, y=99999 na barra de
    tarefas. Recusar e o unico comportamento que nao clica em algo que o
    agente nao pediu.
    """

    def __init__(self, x, y, motivo, **detalhe):
        super().__init__(f"Coordenada ({x}, {y}) fora da tela: {motivo}")
        self.x, self.y, self.motivo, self.detalhe = x, y, motivo, detalhe

    def resposta(self, acao=None):
        return {"ok": False, "error_code": "coordinates_out_of_bounds",
                "action": acao, "x": self.x, "y": self.y, "reason": self.motivo,
                "error": str(self), **self.detalhe}


def exigir_ponto_na_tela(x, y, monitor_rect=None):
    """Levanta CoordenadaForaDaTela se (x, y) não é pixel de monitor nenhum.

    A tela virtual e so o retangulo que envolve os monitores; com monitores de
    tamanhos ou alturas diferentes ela tem cantos sem pixel. MonitorFromPoint
    responde a pergunta certa. `monitor_rect` exige, alem disso, que o ponto
    fique no monitor que o chamador enderecou.
    """
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        raise CoordenadaForaDaTela(x, y, "coordenada nao numerica")
    if monitor_rect is not None:
        ox, oy, mw, mh = monitor_rect
        if not (ox <= x < ox + mw and oy <= y < oy + mh):
            raise CoordenadaForaDaTela(x, y, "fora do monitor enderecado",
                                       monitor_rect=[ox, oy, mw, mh])
    if not user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONULL):
        raise CoordenadaForaDaTela(x, y, "nenhum monitor contem o ponto")
    return x, y


# =====================================================================
# EXCLUSAO DE JANELAS DO PROPRIO AGENTE
# =====================================================================
# Medido: com o terminal do agente sobreposto ao alvo, 45 de 113 marcas do SoM
# vieram do terminal - inclusive o texto que o proprio agente escreveu. Um
# click_text numa frase dele podia clicar no terminal em vez do alvo.
#
# Regras (acordadas com a revisao externa):
# - filtro por PID e/ou nome de processo;
# - o alvo EXPLICITO (hwnd/janela do pedido) nunca e excluido;
# - a janela em foco so e excluida se o PID dela foi listado: casar apenas por
#   nome (filtro amplo) nao basta para esconder o que o usuario esta usando;
# - marcas dessas janelas saem do SoM, e nenhum clique resolve alvo dentro
#   delas nem clica "atraves" delas;
# - a regiao que elas cobrem e informada como `occluded`.

_exclusao = {
    "pids": {int(p) for p in os.environ.get("SOM_EXCLUDE_PIDS", "").split(",") if p.strip().isdigit()},
    "processos": {p.strip().lower() for p in os.environ.get("SOM_EXCLUDE_PROCESSES", "").split(",")
                  if p.strip()},
}
_exclusao_lock = threading.Lock()
GA_ROOT = 2


class AlvoOcluido(ValueError):
    """O ponto pedido está coberto por uma janela excluída."""

    def __init__(self, x, y, janela):
        super().__init__(f"Ponto ({x}, {y}) coberto por janela excluida "
                         f"({janela.get('process')}, pid {janela.get('pid')})")
        self.x, self.y, self.janela = x, y, janela

    def resposta(self, acao=None):
        return {"ok": False, "error_code": "target_occluded", "action": acao,
                "x": self.x, "y": self.y, "occluded_by": self.janela, "error": str(self)}


def configurar_exclusao(pids=None, processos=None):
    """Troca a lista de exclusão em tempo de execução. Listas vazias limpam."""
    with _exclusao_lock:
        _exclusao["pids"] = {int(p) for p in (pids or []) if str(p).strip().isdigit()}
        _exclusao["processos"] = {str(p).strip().lower() for p in (processos or []) if str(p).strip()}
        return {"pids": sorted(_exclusao["pids"]), "processes": sorted(_exclusao["processos"])}


def exclusao_ativa():
    with _exclusao_lock:
        return bool(_exclusao["pids"] or _exclusao["processos"])


class _JuizDeExclusao:
    """Decide, com cache por captura, se uma janela raiz está excluída."""

    def __init__(self, alvo_explicito=None):
        with _exclusao_lock:
            self.pids = set(_exclusao["pids"])
            self.processos = set(_exclusao["processos"])
        self.alvo = int(alvo_explicito or 0)
        self.foreground = int(user32.GetForegroundWindow() or 0)
        self._nome = {}
        self._decisao = {}

    @property
    def ativo(self):
        return bool(self.pids or self.processos)

    def _pid(self, hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        return int(pid.value)

    def excluida(self, raiz):
        raiz = int(raiz or 0)
        if not raiz or not self.ativo or raiz == self.alvo:
            return False
        if raiz in self._decisao:
            return self._decisao[raiz]
        pid = self._pid(raiz)
        por_pid = pid in self.pids
        if not por_pid and self.processos:
            if pid not in self._nome:
                self._nome[pid] = str(process_name_from_pid(pid) or "").lower()
            por_nome = self._nome[pid] in self.processos
        else:
            por_nome = False
        # A janela em foco so cai por PID: nome e filtro amplo demais para
        # esconder o que o usuario esta usando.
        decisao = por_pid or (por_nome and raiz != self.foreground)
        self._decisao[raiz] = decisao
        return decisao

    def raiz_no_ponto(self, x, y):
        h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        return int(user32.GetAncestor(h, GA_ROOT) or h or 0) if h else 0

    def ponto_excluido(self, x, y):
        raiz = self.raiz_no_ponto(x, y)
        return raiz if self.excluida(raiz) else 0

    def descrever(self, raiz):
        d = describe_window(raiz) or {}
        return {"hwnd": raiz, "pid": d.get("pid"), "process": d.get("process"),
                "rect": [d.get("left"), d.get("top"), d.get("width"), d.get("height")]}


def exigir_nao_ocluido(x, y, alvo_explicito=None):
    """Levanta AlvoOcluido se o ponto cai numa janela excluída."""
    if not exclusao_ativa():
        return
    juiz = _JuizDeExclusao(alvo_explicito)
    raiz = juiz.ponto_excluido(x, y)
    if raiz:
        raise AlvoOcluido(int(x), int(y), juiz.descrever(raiz))


def regioes_ocluidas(monitor_rect, alvo_explicito=None):
    """Janelas excluídas visíveis que cobrem parte do monitor capturado."""
    if not exclusao_ativa():
        return []
    juiz = _JuizDeExclusao(alvo_explicito)
    ox, oy, mw, mh = monitor_rect
    saida = []
    janelas = get_open_windows()        # EnumWindows: de cima para baixo no z-order
    alvo = juiz.alvo or juiz.foreground
    pos_alvo = next((i for i, w in enumerate(janelas) if int(w.get("hwnd") or 0) == alvo), None)
    if pos_alvo is not None and juiz.excluida(alvo):
        pos_alvo = None                  # o "alvo" e a propria janela excluida: nao ha alvo
    for i, w in enumerate(janelas):
        if not juiz.excluida(w.get("hwnd")):
            continue
        if pos_alvo is not None and i > pos_alvo:
            continue                     # atras do alvo: nao cobre nada dele
        l, t = max(ox, w["left"]), max(oy, w["top"])
        r, b = min(ox + mw, w["left"] + w["width"]), min(oy + mh, w["top"] + w["height"])
        if r > l and b > t:
            saida.append({"hwnd": w["hwnd"], "pid": w.get("pid"), "process": w.get("process"),
                          "rect": [l, t, r - l, b - t]})
    return saida


# Teclado vai para a janela em foco. Se ela e uma janela excluida (o terminal do
# agente), digitar la e exatamente o erro que a exclusao existe para impedir -
# e nao ha "outra janela por baixo" para onde desviar em silencio.
ACOES_DE_TECLADO = {"type", "paste", "key", "hotkey", "keydown", "keyup"}


def _recusa_teclado_em_janela_excluida(body):
    acao = str(body.get("action") or "")
    if acao not in ACOES_DE_TECLADO or not exclusao_ativa() or has_window_target(body):
        return None                      # com janela nomeada, a acao da foco nela antes
    juiz = _JuizDeExclusao()
    if not juiz.foreground or not juiz.excluida(juiz.foreground):
        return None
    return {"ok": False, "error_code": "target_excluded", "action": acao,
            "actionDispatched": False, "outcomeVerified": False, "outcome": "not_dispatched",
            "foreground": juiz.descrever(juiz.foreground),
            "error": ("A janela em foco esta excluida (e do proprio agente): o teclado "
                      "iria para ela. Foque o alvo antes, ou passe window/hwnd no pedido.")}


def win32_coords_to_normalized(x, y):
    v_left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    v_top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    v_width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    v_height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
    if v_width <= 0:
        v_width = user32.GetSystemMetrics(0)
    if v_height <= 0:
        v_height = user32.GetSystemMetrics(1)
    # O Windows converte de volta com pixel = floor(norm * largura / 65536).
    # int(x * 65535 / largura) fica um fio abaixo de x e o floor leva a x-1:
    # medido nesta maquina, 1 de 52 pontos caia exato, o resto 1 px acima e a
    # esquerda - todo move e todo clique. O TETO de x * 65536 / largura e o
    # menor valor que volta para x, e continua certo se a conversao
    # arredondar em vez de truncar (a fracao fica abaixo de largura/65536).
    v_width = max(1, v_width)
    v_height = max(1, v_height)
    norm_x = min(65535, max(0, -(-(int(x) - v_left) * 65536 // v_width)))
    norm_y = min(65535, max(0, -(-(int(y) - v_top) * 65536 // v_height)))
    return norm_x, norm_y

def _get_mouse_button_flags(button):
    btn = str(button).lower().strip()
    if btn == "right":
        return MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    elif btn == "middle":
        return MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
    return MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP

def win32_mouse_move(x, y, relative=False):
    ensure_desktop_access()
    inp = INPUT()
    inp.type = INPUT_MOUSE
    if relative:
        inp.u.mi.dx = int(x)
        inp.u.mi.dy = int(y)
        inp.u.mi.dwFlags = MOUSEEVENTF_MOVE
    else:
        nx, ny = win32_coords_to_normalized(int(x), int(y))
        inp.u.mi.dx = nx
        inp.u.mi.dy = ny
        inp.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    _marcar_entrada_enviada()
    return user32.SendInput(1, byref(inp), ctypes.sizeof(INPUT)) == 1

def win32_mouse_down(button="left", x=None, y=None):
    ensure_desktop_access()
    if x is not None and y is not None:
        exigir_nao_ocluido(x, y)     # antes de apertar: nada fica preso
    inputs = []
    if x is not None and y is not None:
        nx, ny = win32_coords_to_normalized(int(x), int(y))
        m = INPUT()
        m.type = INPUT_MOUSE
        m.u.mi.dx = nx
        m.u.mi.dy = ny
        m.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        inputs.append(m)
    d_flag, _ = _get_mouse_button_flags(button)
    d = INPUT()
    d.type = INPUT_MOUSE
    d.u.mi.dwFlags = d_flag
    inputs.append(d)
    arr = (INPUT * len(inputs))(*inputs)
    _marcar_entrada_enviada()
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) == len(inputs)

def win32_mouse_up(button="left", x=None, y=None):
    ensure_desktop_access()
    inputs = []
    if x is not None and y is not None:
        nx, ny = win32_coords_to_normalized(int(x), int(y))
        m = INPUT()
        m.type = INPUT_MOUSE
        m.u.mi.dx = nx
        m.u.mi.dy = ny
        m.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        inputs.append(m)
    _, u_flag = _get_mouse_button_flags(button)
    u = INPUT()
    u.type = INPUT_MOUSE
    u.u.mi.dwFlags = u_flag
    inputs.append(u)
    arr = (INPUT * len(inputs))(*inputs)
    _marcar_entrada_enviada()
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) == len(inputs)

def win32_mouse_click(x=None, y=None, button="left", clicks=1):
    ensure_desktop_access()
    if x is not None and y is not None:
        # Ponto unico por onde passa todo clique - por tag, texto, coordenada
        # ou revalidado: nenhum atravessa uma janela excluida.
        exigir_nao_ocluido(x, y)
    d_flag, u_flag = _get_mouse_button_flags(button)
    inputs = []
    if x is not None and y is not None:
        nx, ny = win32_coords_to_normalized(int(x), int(y))
        m = INPUT()
        m.type = INPUT_MOUSE
        m.u.mi.dx = nx
        m.u.mi.dy = ny
        m.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        inputs.append(m)
    for _ in range(max(1, clicks)):
        d = INPUT()
        d.type = INPUT_MOUSE
        d.u.mi.dwFlags = d_flag
        inputs.append(d)
        u = INPUT()
        u.type = INPUT_MOUSE
        u.u.mi.dwFlags = u_flag
        inputs.append(u)
    arr = (INPUT * len(inputs))(*inputs)
    _marcar_entrada_enviada()
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) == len(inputs)

def win32_mouse_scroll(dy, dx=0, x=None, y=None):
    ensure_desktop_access()
    if x is not None and y is not None:
        exigir_nao_ocluido(x, y)
    inputs = []
    if x is not None and y is not None:
        nx, ny = win32_coords_to_normalized(int(x), int(y))
        m = INPUT()
        m.type = INPUT_MOUSE
        m.u.mi.dx = nx
        m.u.mi.dy = ny
        m.u.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        inputs.append(m)
    if dy != 0:
        s = INPUT()
        s.type = INPUT_MOUSE
        s.u.mi.mouseData = int(dy)
        s.u.mi.dwFlags = MOUSEEVENTF_WHEEL
        inputs.append(s)
    if dx != 0:
        h = INPUT()
        h.type = INPUT_MOUSE
        h.u.mi.mouseData = int(dx)
        h.u.mi.dwFlags = MOUSEEVENTF_HWHEEL
        inputs.append(h)
    if not inputs:
        return True
    arr = (INPUT * len(inputs))(*inputs)
    _marcar_entrada_enviada()
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) == len(inputs)

def win32_mouse_smooth_move(target_x, target_y, duration=0.25, tween_fn=None):
    ensure_desktop_access()
    curr_pos = POINT()
    user32.GetCursorPos(byref(curr_pos))
    start_x, start_y = curr_pos.x, curr_pos.y
    duration = max(0.01, float(duration))
    steps = max(6, int(duration * 60))
    dt = duration / steps
    if tween_fn is None:
        tween_fn = getattr(pyautogui, "easeInOutQuad", lambda n: n)
    for i in range(1, steps + 1):
        t = i / steps
        factor = tween_fn(t)
        cur_x = int(start_x + (target_x - start_x) * factor)
        cur_y = int(start_y + (target_y - start_y) * factor)
        win32_mouse_move(cur_x, cur_y)
        time.sleep(dt)
    win32_mouse_move(target_x, target_y)
    return True

# O anexo ao desktop vale por thread, mas o handle so precisa ser aberto uma vez
# nela. Reabrir a cada chamada vazava um handle por frame capturado (~20/s no
# /stream) porque o handle anterior era sobrescrito sem CloseDesktop.
_desktop_tls = threading.local()

def ensure_desktop_access():
    """Garante que a thread atual esteja anexada ao desktop interativo do usuário físico."""
    if getattr(_desktop_tls, "attached", False):
        return
    try:
        # Priorizar OpenInputDesktop (desktop atualmente visível e interagível pelo usuário)
        hDesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if not hDesk:
            hDesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if hDesk:
            # SetThreadDesktop falha se a thread ja possui janelas ou hooks; nesse
            # caso o handle recem-aberto nao serve para nada e precisa ser fechado.
            if user32.SetThreadDesktop(hDesk):
                _desktop_tls.handle = hDesk
                _desktop_tls.attached = True
            else:
                user32.CloseDesktop(hDesk)
    except Exception:
        pass

def release_desktop_access():
    """Fecha o handle de desktop da thread atual; usado ao fim de cada worker."""
    handle = getattr(_desktop_tls, "handle", None)
    if handle:
        try:
            user32.CloseDesktop(handle)
        except Exception:
            pass
    _desktop_tls.handle = None
    _desktop_tls.attached = False

def run_in_desktop_thread(func, *args, **kwargs):
    """Executa qualquer função em thread limpa com anexo garantido ao desktop interativo."""
    res = [None]
    err = [None]
    def _runner():
        ensure_desktop_access()
        try:
            res[0] = func(*args, **kwargs)
        except Exception as e:
            err[0] = e
        finally:
            # A thread morre aqui: sem isso o handle de desktop dela ficaria orfao.
            release_desktop_access()
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        log.error("run_in_desktop_thread: timeout de 30s em %s — thread abandonada", getattr(func, '__name__', func))
        raise TimeoutError(f"Ação '{getattr(func, '__name__', func)}' excedeu 30s e foi abandonada.")
    if err[0]:
        raise err[0]
    return res[0]


def get_monitors():
    ensure_desktop_access()
    mons = []
    def _cb(hMon, hdc, lprc, lparam):
        r = lprc.contents
        mons.append({
            "id": str(len(mons) + 1),
            "left": int(r.left),
            "top": int(r.top),
            "right": int(r.right),
            "bottom": int(r.bottom),
            "width": int(r.right - r.left),
            "height": int(r.bottom - r.top)
        })
        return 1
    CB = MONITORENUMPROC(_cb)
    user32.EnumDisplayMonitors(None, None, CB, 0)
    return mons

def get_monitor_geom(mon_choice="2"):
    mons = get_monitors()
    if not mons:
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if str(mon_choice) == "1":
        m = mons[0]
        return m["left"], m["top"], m["width"], m["height"]
    elif str(mon_choice) == "2" and len(mons) > 1:
        m = mons[1]
        return m["left"], m["top"], m["width"], m["height"]
    elif str(mon_choice) == "all" and len(mons) > 1:
        left = min(m["left"] for m in mons)
        top = min(m["top"] for m in mons)
        right = max(m["right"] for m in mons)
        bottom = max(m["bottom"] for m in mons)
        return left, top, right - left, bottom - top
    else:
        m = mons[0]
        return m["left"], m["top"], m["width"], m["height"]

# Gestão nativa de janelas (Win32)
def process_name_from_pid(pid):
    """Resolve o executável de um PID sempre fechando o handle do processo."""
    if not pid:
        return "unknown"
    h_proc = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h_proc:
        return "unknown"
    try:
        nbuf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, nbuf, byref(size)):
            return os.path.basename(nbuf.value)
    finally:
        # O finally garante o fechamento mesmo se a consulta levantar: antes, um
        # erro aqui deixava o handle do processo aberto para sempre.
        kernel32.CloseHandle(h_proc)
    return "unknown"


def describe_window(hwnd):
    """Descreve uma janela específica sem precisar varrer todas as outras."""
    # Mesma classe do bug corrigido em 01b2f29: thread nao anexada ao desktop de
    # entrada recebe 0 de GetForegroundWindow e retangulo vazio de GetWindowRect.
    # Aqui pesa mais, porque get_foreground_window_info() passa por aqui a cada
    # publish_state_snapshot e a cada validate_frame_reference. Depois da
    # primeira vez na thread isto e so um getattr.
    ensure_desktop_access()
    try:
        hwnd = int(hwnd or 0)
    except (ValueError, TypeError):
        return None
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    rect = RECT()
    user32.GetWindowRect(hwnd, byref(rect))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, byref(pid))
    return {
        "hwnd": hwnd,
        "title": buf.value,
        "pid": pid.value,
        "process": process_name_from_pid(pid.value),
        "left": rect.left,
        "top": rect.top,
        "width": max(1, rect.right - rect.left),
        "height": max(1, rect.bottom - rect.top),
    }


def has_window_target(body):
    """Diz se o payload traz algum identificador de janela de fato preenchido."""
    return any(body.get(key) for key in
               ("focus", "window", "title", "hwnd", "process", "process_name"))


def get_open_windows():
    ensure_desktop_access()
    wins = []
    def _enum(h, lparam):
        if user32.IsWindowVisible(h):
            length = user32.GetWindowTextLengthW(h)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(h, buf, length + 1)
                rect = RECT()
                user32.GetWindowRect(h, byref(rect))
                w = rect.right - rect.left
                h_len = rect.bottom - rect.top
                if w > 40 and h_len > 40:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(h, byref(pid))
                    pname = process_name_from_pid(pid.value)
                    wins.append({
                        "hwnd": int(h),
                        "title": buf.value,
                        "pid": pid.value,
                        "process": pname,
                        "left": rect.left,
                        "top": rect.top,
                        "width": w,
                        "height": h_len
                    })
        return True
    CB = WNDENUMPROC(_enum)
    user32.EnumWindows(CB, 0)
    return wins

def focus_window(title_kw=None, hwnd=None, process_name=None):
    ensure_desktop_access()
    target_hwnd = None
    target_win = None
    wins = None

    # Se title_kw for passado como int ou string puramente numérica de HWND
    if title_kw is not None and hwnd is None:
        if isinstance(title_kw, int):
            hwnd = title_kw
            title_kw = None
        elif isinstance(title_kw, str) and title_kw.isdigit() and len(title_kw) >= 4:
            hwnd = int(title_kw)
            title_kw = None

    if hwnd:
        try:
            target_hwnd = int(hwnd)
            # Com o HWND em maos nao ha motivo para enumerar todas as janelas.
            target_win = describe_window(target_hwnd)
        except (ValueError, TypeError):
            target_hwnd = None
    elif process_name:
        cache_key = f"proc:{str(process_name).lower().strip()}"
        cached_hwnd, cached_win = _hwnd_cache_get(cache_key)
        if cached_hwnd:
            target_hwnd, target_win = cached_hwnd, cached_win
            log.debug("focus_window: cache hit process '%s' hwnd=%s", process_name, target_hwnd)
        else:
            wins = get_open_windows()
            p_clean = str(process_name).lower().strip().replace(".exe", "")
            for w in wins:
                w_proc = w.get("process", "").lower().replace(".exe", "")
                if p_clean == w_proc or p_clean in w_proc:
                    target_hwnd = w["hwnd"]
                    target_win = w
                    break
            if target_hwnd:
                _hwnd_cache_set(cache_key, target_hwnd, target_win)
    elif title_kw:
        cache_key = f"title:{str(title_kw).lower().strip()}"
        cached_hwnd, cached_win = _hwnd_cache_get(cache_key)
        if cached_hwnd:
            target_hwnd, target_win = cached_hwnd, cached_win
            log.debug("focus_window: cache hit title '%s' hwnd=%s", title_kw, target_hwnd)
        else:
            wins = get_open_windows()
            kw = str(title_kw).lower().strip()
            # 1. Match exato por substring no título
            for w in wins:
                if kw in w["title"].lower():
                    target_hwnd = w["hwnd"]
                    target_win = w
                    break
            # 2. Fallback: match pelo nome do executável/processo
            if not target_hwnd:
                for w in wins:
                    w_proc = w.get("process", "").lower().replace(".exe", "")
                    if kw == w_proc or kw in w_proc:
                        target_hwnd = w["hwnd"]
                        target_win = w
                        break
            # 3. Fallback: match parcial por tokens de palavras
            if not target_hwnd:
                tokens = [t for t in kw.split() if len(t) > 2]
                if tokens:
                    for w in wins:
                        w_title = w["title"].lower()
                        if any(tok in w_title for tok in tokens):
                            target_hwnd = w["hwnd"]
                            target_win = w
                            break
            if target_hwnd:
                _hwnd_cache_set(cache_key, target_hwnd, target_win)


    if target_hwnd:
        # Uma mesma acao chama focus_window duas vezes (no topo de
        # execute_system_action e de novo em coords_from_payload). Repetir a
        # danca de foco custava um Alt sintetico e 80 ms a toa, e o Alt extra
        # chegava a abrir menu em app que escuta a tecla sozinha.
        already_foreground = (
            int(user32.GetForegroundWindow() or 0) == target_hwnd
            and not user32.IsIconic(target_hwnd)
        )
        if not already_foreground:
            if user32.IsIconic(target_hwnd):
                user32.ShowWindow(target_hwnd, 9) # SW_RESTORE (restaura se minimizada)
            fore_hwnd = user32.GetForegroundWindow()
            cur_tid = kernel32.GetCurrentThreadId()
            fore_tid = user32.GetWindowThreadProcessId(fore_hwnd, None)
            target_tid = user32.GetWindowThreadProcessId(target_hwnd, None)

            # Conectar input threads para autorizar troca de foco
            if cur_tid != fore_tid and fore_tid != 0:
                user32.AttachThreadInput(cur_tid, fore_tid, True)
            if cur_tid != target_tid and target_tid != 0:
                user32.AttachThreadInput(cur_tid, target_tid, True)

            try:
                # Bypass do bloqueio de SetForegroundWindow do Windows 10/11
                user32.AllowSetForegroundWindow(0xFFFFFFFF)
                VK_MENU = 0x12
                KEYEVENTF_KEYUP = 0x0002
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                user32.BringWindowToTop(target_hwnd)
                user32.SetForegroundWindow(target_hwnd)
            finally:
                if cur_tid != fore_tid and fore_tid != 0:
                    user32.AttachThreadInput(cur_tid, fore_tid, False)
                if cur_tid != target_tid and target_tid != 0:
                    user32.AttachThreadInput(cur_tid, target_tid, False)
            time.sleep(0.08)
        if not target_win:
            target_win = {"hwnd": target_hwnd}
        if "left" not in target_win:
            r = RECT()
            user32.GetWindowRect(target_hwnd, byref(r))
            target_win["left"] = r.left
            target_win["top"] = r.top
            target_win["width"] = max(1, r.right - r.left)
            target_win["height"] = max(1, r.bottom - r.top)
        return True, target_win
    return False, None

def coords_from_payload(body, default_monitor="1"):
    """Coordenada de tela do pedido, recusada se não cair em monitor nenhum.

    E aqui que coordenada vinda do agente entra no servidor; marcas, templates
    e UIA ja nascem de pixels da tela. A checagem nao fica na primitiva de
    SendInput porque o movimento suave passa por pontos intermediarios, e a
    reta entre dois pontos validos pode cruzar um canto sem monitor.
    """
    real_x, real_y, monitor = _coords_from_payload_bruto(body, default_monitor)
    enderecado = None
    if monitor != "window" and not body.get("absolute", False) and (
            "monitor" in body or ("rx" in body and "ry" in body)):
        enderecado = get_monitor_geom(monitor)
    exigir_ponto_na_tela(real_x, real_y, enderecado)
    exigir_nao_ocluido(real_x, real_y, body.get("hwnd"))
    return real_x, real_y, monitor


def _coords_from_payload_bruto(body, default_monitor="1"):
    # Suporte a coordenadas relativas à área cliente da janela (ClientToScreen)
    if ("client_x" in body or "cx" in body or "client_rx" in body or "crx" in body) and ("client_y" in body or "cy" in body or "client_ry" in body or "cry" in body):
        kw = body.get("window") or body.get("focus")
        hwnd_val = body.get("hwnd")
        proc_val = body.get("process") or body.get("process_name")
        ok, target_win = focus_window(title_kw=kw, hwnd=hwnd_val, process_name=proc_val)
        if ok and target_win and target_win.get("hwnd"):
            thwnd = target_win["hwnd"]
            rc = RECT()
            user32.GetClientRect(thwnd, byref(rc))
            client_w = max(1, rc.right - rc.left)
            client_h = max(1, rc.bottom - rc.top)
            if "client_rx" in body or "crx" in body:
                cx = int(float(body.get("client_rx", body.get("crx", 0.0))) * client_w)
                cy = int(float(body.get("client_ry", body.get("cry", 0.0))) * client_h)
            else:
                cx = int(body.get("client_x", body.get("cx", 0)))
                cy = int(body.get("client_y", body.get("cy", 0)))
            pt = POINT(cx, cy)
            if user32.ClientToScreen(thwnd, byref(pt)):
                return pt.x, pt.y, "client"

    target_win = None
    if has_window_target(body):
        kw = body.get("window") or body.get("focus")
        hwnd_val = body.get("hwnd")
        proc_val = body.get("process") or body.get("process_name")
        ok, target_win = focus_window(title_kw=kw, hwnd=hwnd_val, process_name=proc_val)

    if target_win and ("wx" in body or "wrx" in body):
        thwnd = target_win.get("hwnd")
        # Se tiver HWND, verifica coordenadas da área cliente para evitar bug de borda negativa de janelas maximizadas (-9, -9)
        if thwnd:
            rc = RECT()
            user32.GetClientRect(thwnd, byref(rc))
            pt_zero = POINT(0, 0)
            user32.ClientToScreen(thwnd, byref(pt_zero))
            wl = pt_zero.x
            wt = pt_zero.y
            ww = max(1, rc.right - rc.left)
            wh = max(1, rc.bottom - rc.top)
        else:
            wl = target_win.get("left", 0)
            wt = target_win.get("top", 0)
            ww = target_win.get("width", 1920)
            wh = target_win.get("height", 1080)

        if "wrx" in body and "wry" in body:
            real_x = wl + int(float(body["wrx"]) * ww)
            real_y = wt + int(float(body["wry"]) * wh)
        else:
            real_x = wl + int(body.get("wx", 0))
            real_y = wt + int(body.get("wy", 0))
        return real_x, real_y, "window"

    monitor = str(body.get("monitor", default_monitor))
    ox, oy, mw, mh = get_monitor_geom(monitor)
    
    if "rx" in body and "ry" in body:
        rx = float(body["rx"])
        ry = float(body["ry"])
        # rx=1.0 e a borda direita, nao o pixel depois dela. So o 1.0 exato:
        # rx=5 continua fora e a validacao recusa, em vez de prender na borda.
        real_x = ox + (mw - 1 if rx == 1.0 else int(rx * mw))
        real_y = oy + (mh - 1 if ry == 1.0 else int(ry * mh))
    else:
        # Se 'x' e 'y' forem passados e 'absolute': True, mantemos o valor bruto de tela virtual.
        # Caso contrário, se monitor foi especificado (ou padrão), somamos o offset do monitor ox e oy
        raw_x = int(body.get("x", 0))
        raw_y = int(body.get("y", 0))
        if body.get("absolute", False) or "monitor" not in body:
            real_x = raw_x
            real_y = raw_y
        else:
            real_x = ox + raw_x
            real_y = oy + raw_y
    return real_x, real_y, monitor

# Desenho do hardware cursor sobre o frame capturado com compensação exata de Hotspot
def draw_hardware_cursor(hdc_dest, src_x, src_y, scale=1.0):
    try:
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if user32.GetCursorInfo(byref(ci)):
            if ci.flags & 1:  # CURSOR_SHOWING
                hotspot_x = 0
                hotspot_y = 0
                ii = ICONINFO()
                if user32.GetIconInfo(ci.hCursor, byref(ii)):
                    hotspot_x = int(ii.xHotspot)
                    hotspot_y = int(ii.yHotspot)
                    if ii.hbmMask:
                        gdi32.DeleteObject(ii.hbmMask)
                    if ii.hbmColor:
                        gdi32.DeleteObject(ii.hbmColor)
                cx = int((ci.ptScreenPos.x - hotspot_x - src_x) * scale)
                cy = int((ci.ptScreenPos.y - hotspot_y - src_y) * scale)
                user32.DrawIconEx(
                    hdc_dest, cx, cy, ci.hCursor, 0, 0, 0, None, 3  # DI_NORMAL
                )
    except Exception:
        pass

# Captura de alta performance via Win32 GDI com Cursor Real Renderizado
def capture_screen_fast(quality=90, scale=1.0, monitor="1", draw_cursor=True):
    ensure_desktop_access()

    src_x, src_y, src_w, src_h = get_monitor_geom(monitor)
    dst_w = max(1, int(src_w * scale))
    dst_h = max(1, int(src_h * scale))

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbm = None
    old_bmp = None

    try:
        if not hdc_screen or not hdc_mem:
            raise Exception("GDI DC creation failed")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = dst_w
        bmi.bmiHeader.biHeight = -dst_h  # Top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24    # BGR 24-bit
        bmi.bmiHeader.biCompression = 0

        p_bits = c_void_p()
        hbm = gdi32.CreateDIBSection(hdc_screen, byref(bmi), 0, byref(p_bits), None, 0)
        if not hbm or not p_bits.value:
            raise Exception("CreateDIBSection failed")

        old_bmp = gdi32.SelectObject(hdc_mem, hbm)
        gdi32.SetStretchBltMode(hdc_mem, 3) # COLORONCOLOR

        stride = ((dst_w * 3 + 3) & ~3)
        buf_size = stride * dst_h

        gdi32.StretchBlt(hdc_mem, 0, 0, dst_w, dst_h, hdc_screen, src_x, src_y, src_w, src_h, 0x00CC0020)

        if draw_cursor:
            draw_hardware_cursor(hdc_mem, src_x, src_y, scale)

        raw = ctypes.string_at(p_bits.value, buf_size)
        img = Image.frombytes('RGB', (dst_w, dst_h), raw, 'raw', 'BGR', stride, 1)
    except Exception:
        img = ImageGrab.grab(bbox=(src_x, src_y, src_x + src_w, src_y + src_h))
        if scale != 1.0:
            img = img.resize((dst_w, dst_h), Image.Resampling.BILINEAR)
    finally:
        # O DIB tem que sair do DC antes de ser destruido, senao o DeleteObject
        # falha e o handle vaza. E no caminho de erro o bitmap nao era liberado
        # de jeito nenhum: com o limite de 10k handles GDI, o processo morria.
        if hdc_mem and old_bmp:
            gdi32.SelectObject(hdc_mem, old_bmp)
        if hbm:
            gdi32.DeleteObject(hbm)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        if hdc_screen:
            user32.ReleaseDC(0, hdc_screen)

    # A ordem do OCR/contornos pode mudar a cada captura. Substituímos os IDs
    # temporários por tags persistentes e só então desenhamos o frame final.
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), src_w, src_h

# dxcam exige uma instancia por saida de video. Uma unica instancia global fazia
# o parametro 'monitor' ser ignorado depois da primeira captura, devolvendo
# sempre a tela do monitor inicial.
_dxcam_instances = {}

def capture_raw_pil_image(monitor="1", draw_cursor=False):
    """
    Captura imagem de tela diretamente da GPU (DXGI) ou DIBSection Win32 em memória,
    eliminando 100% do overhead de codificação/decodificação JPEG intermediária.
    """
    ensure_desktop_access()
    src_x, src_y, src_w, src_h = get_monitor_geom(monitor)
    
    # 1. Tentativa via DXCAM (DirectX 11 GPU Framebuffer Duplication)
    # Quando o oráculo DXGISnapshotService está ativo, ele centraliza a duplicação
    # como singleton do processo, evitando concorrência interna na mesma saída.
    if str(monitor).isdigit() and not ENABLE_DXGI_ORACLE:
        out_idx = max(0, int(monitor) - 1)
        try:
            import dxcam
            camera = _dxcam_instances.get(out_idx)
            if camera is None:
                camera = dxcam.create(output_idx=out_idx)
                _dxcam_instances[out_idx] = camera
            frame = camera.grab()
            if frame is not None:
                return Image.fromarray(frame)
        except Exception:
            # Descartar a instancia sem release() segura o duplicador DXGI e
            # impede qualquer captura futura naquela saida.
            stale = _dxcam_instances.pop(out_idx, None)
            if stale is not None:
                for method in ("release", "stop"):
                    try:
                        getattr(stale, method)()
                    except Exception:
                        pass

    # 2. Fallback imediato: DIBSection Win32 direto na memória RAM
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbm = None
    old_bmp = None
    try:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = src_w
        bmi.bmiHeader.biHeight = -src_h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = 0

        p_bits = c_void_p()
        hbm = gdi32.CreateDIBSection(hdc_screen, byref(bmi), 0, byref(p_bits), None, 0)
        if not hbm or not p_bits.value:
            raise Exception("CreateDIBSection failed")
        old_bmp = gdi32.SelectObject(hdc_mem, hbm)
        gdi32.SetStretchBltMode(hdc_mem, 3)

        stride = ((src_w * 3 + 3) & ~3)
        buf_size = stride * src_h
        gdi32.StretchBlt(hdc_mem, 0, 0, src_w, src_h, hdc_screen, src_x, src_y, src_w, src_h, 0x00CC0020)

        if draw_cursor:
            draw_hardware_cursor(hdc_mem, src_x, src_y, 1.0)

        raw = ctypes.string_at(p_bits.value, buf_size)
        return Image.frombytes('RGB', (src_w, src_h), raw, 'raw', 'BGR', stride, 1)
    except Exception:
        return ImageGrab.grab(bbox=(src_x, src_y, src_x + src_w, src_y + src_h))
    finally:
        if hdc_mem and old_bmp:
            gdi32.SelectObject(hdc_mem, old_bmp)
        if hbm:
            gdi32.DeleteObject(hbm)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        if hdc_screen:
            user32.ReleaseDC(0, hdc_screen)

def capture_pil_image(monitor="1", draw_cursor=False):
    return capture_raw_pil_image(monitor=monitor, draw_cursor=draw_cursor)

def get_screen_crop(bbox=None, window_kw=None, hwnd=None, process_name=None, scale=1.0, quality=95, format="jpeg", monitor="1"):
    """Recorta uma Região de Interesse (ROI) em alta definição para modelos de visão IA."""
    ensure_desktop_access()
    target_rect = None
    target_win = None
    raw_bytes, mw, mh = capture_screen_fast(quality=100, scale=1.0, monitor=monitor, draw_cursor=False)
    full_img = Image.open(io.BytesIO(raw_bytes))
    img_w, img_h = full_img.size
    ox, oy, _, _ = get_monitor_geom(monitor)

    if window_kw or hwnd or process_name:
        ok, target_win = focus_window(title_kw=window_kw, hwnd=hwnd, process_name=process_name)
        if ok and target_win:
            target_rect = [
                int(target_win["left"] - ox),
                int(target_win["top"] - oy),
                int(target_win["width"]),
                int(target_win["height"])
            ]

    if bbox:
        if isinstance(bbox, str):
            bbox = [float(v.strip()) for v in bbox.split(",") if v.strip()]
        if len(bbox) == 4:
            b0, b1, b2, b3 = bbox
            if max(b0, b1, b2, b3) <= 1.0:
                bx = int(b0 * img_w)
                by = int(b1 * img_h)
                bw = int(b2 * img_w)
                bh = int(b3 * img_h)
            else:
                if b0 >= ox and ox != 0:
                    bx = int(b0 - ox)
                    by = int(b1 - oy)
                else:
                    bx = int(b0)
                    by = int(b1)
                bw = int(b2)
                bh = int(b3)
            if bw > bx and bh > by and (b2 > img_w * 0.9 or b3 > img_h * 0.9):
                bw = bw - bx
                bh = bh - by
            target_rect = [bx, by, bw, bh]

    if not target_rect:
        target_rect = [0, 0, img_w, img_h]

    rx, ry, rw, rh = target_rect
    x1 = max(0, min(img_w - 1, rx))
    y1 = max(0, min(img_h - 1, ry))
    x2 = max(x1 + 1, min(img_w, rx + rw))
    y2 = max(y1 + 1, min(img_h, ry + rh))

    cropped = full_img.crop((x1, y1, x2, y2))
    scale = max(0.1, min(4.0, float(scale)))
    if scale != 1.0:
        new_w = max(1, int((x2 - x1) * scale))
        new_h = max(1, int((y2 - y1) * scale))
        cropped = cropped.resize((new_w, new_h), Image.Resampling.LANCZOS)

    out_fmt = "PNG" if str(format).upper() == "PNG" else "JPEG"
    mime = "image/png" if out_fmt == "PNG" else "image/jpeg"
    buf = io.BytesIO()
    if out_fmt == "PNG":
        cropped.save(buf, format="PNG")
    else:
        cropped.save(buf, format="JPEG", quality=quality)

    crop_bytes = buf.getvalue()
    real_x = ox + x1
    real_y = oy + y1
    real_w = x2 - x1
    real_h = y2 - y1

    return {
        "ok": True,
        "bytes": crop_bytes,
        "mime": mime,
        "format": out_fmt.lower(),
        "bbox": [real_x, real_y, real_w, real_h],
        "scale": scale,
        "width": cropped.width,
        "height": cropped.height,
        "window": target_win
    }

# =====================================================================
# ARQUITETURA HÍBRIDA: MOTOR UIA (SEMÂNTICO) + MOTOR SOM (VISÃO OCR)
# =====================================================================

current_state_cache = {
    "mode": "none",
    "timestamp": 0,
    "frame_id": None,
    "monitor": None,
    "window": None,
    "elements": [],     # UIA elements
    "marks": [],        # SoM marks
    "last_som_image": None
}

# O servidor HTTP atende requisições em threads diferentes. O lock impede que
# uma captura substitua parcialmente o cache enquanto outra requisição lê tags.
state_cache_lock = threading.RLock()
input_action_lock = threading.RLock()
som_tracker_lock = threading.RLock()
som_trackers = {}
som_trackers_loaded = False
SOM_TRACK_STATE_PATH = os.environ.get(
    "SOM_TRACK_STATE_PATH",
    os.path.join(os.getcwd(), ".remote_control_som_tags.json"),
)
SOM_TRACK_MAX_MISSES = max(1, int(os.environ.get("SOM_TRACK_MAX_MISSES", "30")))
# Intervalo minimo entre gravacoes do estado das tags e teto de tracks por
# monitor (ver _save_som_trackers_locked e assign_stable_som_tags).
SOM_TRACK_SAVE_INTERVAL = max(0.0, float(os.environ.get("SOM_TRACK_SAVE_INTERVAL", "2.0")))
SOM_TRACK_MAX_TRACKS = max(50, int(os.environ.get("SOM_TRACK_MAX_TRACKS", "1500")))
som_trackers_last_save = 0.0
SOM_TRACK_MAX_DISTANCE = max(10.0, float(os.environ.get("SOM_TRACK_MAX_DISTANCE", "90")))
SNAPSHOT_TTL_SECONDS = max(0.5, float(os.environ.get("REMOTE_CONTROL_SNAPSHOT_TTL", "10")))
DEFAULT_STABLE_CAPTURES = max(2, min(5, int(os.environ.get("REMOTE_CONTROL_STABLE_CAPTURES", "2"))))
DEFAULT_STABLE_INTERVAL_MS = max(20, min(500, int(os.environ.get("REMOTE_CONTROL_STABLE_INTERVAL_MS", "80"))))
DEFAULT_STABLE_TIMEOUT_MS = max(100, min(10000, int(os.environ.get("REMOTE_CONTROL_STABLE_TIMEOUT_MS", "1500"))))


# Menor janela que conta como alvo. A "Default IME" que um utilitario de bandeja
# (AcPowerNotification.exe, medido na maquina) deixa com o foco mede 1x1 e e
# invisivel; nenhuma janela de aplicativo real fica abaixo disso.
JANELA_MIN_PX = 8
GA_ROOTOWNER = 3


def janela_utilizavel(descricao):
    """A descrição é de uma janela em que dá para mirar?"""
    return bool(descricao and descricao.get("hwnd")
                and int(descricao.get("width") or 0) >= JANELA_MIN_PX
                and int(descricao.get("height") or 0) >= JANELA_MIN_PX)


def rect_da_janela(descricao):
    """Converte a descrição de janela em (left, top, right, bottom).

    describe_window devolve left/top/width/height; o oraculo DXGI espera as
    quatro bordas. Sem esta traducao `descricao.get("rect")` e sempre None e o
    oraculo cai em tela inteira sem avisar - o filtro por janela vira no-op.
    """
    if not descricao:
        return None
    try:
        esquerda, topo = int(descricao["left"]), int(descricao["top"])
        largura, altura = int(descricao["width"]), int(descricao["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if largura <= 0 or altura <= 0:
        return None
    return (esquerda, topo, esquerda + largura, topo + altura)


def get_foreground_window_info():
    """Descrição da janela em primeiro plano, ou {} se não houver uma de verdade.

    O Windows pode entregar como foreground uma janela oculta: medido nesta
    maquina, a "Default IME" invisivel e 1x1 do AcPowerNotification.exe.
    Aceita-la fazia scope="window" recortar 1 pixel e o oraculo DXGI procurar
    mudanca so nesse pixel - e devolver snapshot velho como se fosse atual.

    Sem janela real, a resposta honesta e {}: o escopo de janela recusa, o
    oraculo olha a tela inteira e validate_frame_reference nao compara contra
    um hwnd falso. NAO se usa a ultima janela focada como reserva - seria dado
    velho apresentado como atual, e anularia o aviso de window_changed.
    """
    # Antes do GetForegroundWindow, nao so dentro do describe_window: em thread
    # nao anexada ao desktop de entrada ele devolve 0.
    ensure_desktop_access()
    alvo = int(user32.GetForegroundWindow() or 0)
    if alvo and not user32.IsWindowVisible(alvo):
        # Janela auxiliar oculta de um app visivel: o app e o alvo real.
        dono = int(user32.GetAncestor(alvo, GA_ROOTOWNER) or 0)
        alvo = dono if dono != alvo and user32.IsWindowVisible(dono) else 0
    if not alvo:
        return {}
    # Visibilidade checada antes: o describe_window faz OpenProcess para ler o
    # nome do processo, e isso e o caro; janela recusada nao paga por ele.
    desc = describe_window(alvo)
    return desc if janela_utilizavel(desc) else {}


# =====================================================================
# ASSINATURA DE ESTADO DE TELA (base da Memoria de Execucao - IDEIAS/02)
# =====================================================================
# Um dHash da area cliente responde "a tela ainda e aquela?" em poucos
# milissegundos, contra 300-1000 ms de uma varredura OCR/SoM completa. E o que
# permite a um passo gravado conferir a tela antes de agir sem chamar modelo.

SIGNATURE_GRID = max(2, min(8, int(os.environ.get("SIGNATURE_GRID", "4"))))
SIGNATURE_TOLERANCE = max(0, int(os.environ.get("SIGNATURE_TOLERANCE", "6")))
SIGNATURE_SAMPLES = max(1, min(10, int(os.environ.get("SIGNATURE_SAMPLES", "3"))))
SIGNATURE_SAMPLE_INTERVAL = max(0.01, float(os.environ.get("SIGNATURE_SAMPLE_INTERVAL", "0.12")))


def _dhash_bits(image, size=8):
    """dHash: compara cada pixel com o vizinho da direita.

    Usamos diferenca entre vizinhos, e nao o valor absoluto, porque isso torna a
    assinatura imune a mudanca global de brilho e a ruido de antialiasing, que
    sao justamente o que mais gera falso negativo em captura de tela.
    """
    small = image.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
    px = list(small.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def _hamming(a, b):
    return bin(a ^ b).count("1")


def _grid_hashes(image, grid=SIGNATURE_GRID):
    """dHash de cada celula da grade, para saber ONDE a tela mudou."""
    width, height = image.size
    cells = []
    for row in range(grid):
        for col in range(grid):
            box = (int(col * width / grid), int(row * height / grid),
                   int((col + 1) * width / grid), int((row + 1) * height / grid))
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                cells.append(0)
                continue
            cells.append(_dhash_bits(image.crop(box)))
    return cells


def _signature_source_image(monitor="1", hwnd=None, window_kw=None, process_name=None):
    """Devolve (imagem, janela, origem) da área cliente alvo, ou do monitor.

    A origem e o ponto de TELA que corresponde ao pixel (0,0) da imagem. Sem ela
    nao da para converter a coordenada de um clique na coordenada do recorte,
    porque a imagem pode estar cortada na area cliente da janela.
    """
    target = None
    if hwnd or window_kw or process_name:
        ok, target = focus_window(title_kw=window_kw, hwnd=hwnd, process_name=process_name)
        if not ok:
            target = None
    if target is None:
        target = get_foreground_window_info()

    image = capture_raw_pil_image(monitor=monitor, draw_cursor=False)
    ox, oy, _mw, _mh = get_monitor_geom(monitor)
    origin = (ox, oy)

    if target and target.get("hwnd"):
        rect = RECT()
        point = POINT(0, 0)
        if user32.GetClientRect(int(target["hwnd"]), byref(rect)) and \
                user32.ClientToScreen(int(target["hwnd"]), byref(point)):
            left = point.x - ox
            top = point.y - oy
            right = left + max(1, rect.right - rect.left)
            bottom = top + max(1, rect.bottom - rect.top)
            left = max(0, min(left, image.size[0] - 1))
            top = max(0, min(top, image.size[1] - 1))
            right = max(left + 1, min(right, image.size[0]))
            bottom = max(top + 1, min(bottom, image.size[1]))
            image = image.crop((left, top, right, bottom))
            origin = (ox + left, oy + top)
    return image, target, origin


def signature_from_image(image, window=None, monitor="1", volatile_cells=None):
    """Monta o dicionário de assinatura a partir de uma imagem já capturada."""
    cells = _grid_hashes(image)
    return {
        "dhash": f"{_dhash_bits(image):016x}",
        "cells": [f"{c:016x}" for c in cells],
        "volatile_cells": sorted(volatile_cells or []),
        "grid": [SIGNATURE_GRID, SIGNATURE_GRID],
        "monitor": str(monitor),
        "samples": 1,
        "window": {
            "hwnd": (window or {}).get("hwnd"),
            "process": (window or {}).get("process"),
            "title": (window or {}).get("title"),
            "client_size": [(window or {}).get("width"), (window or {}).get("height")],
        },
    }


def compute_state_signature(monitor="1", hwnd=None, window_kw=None, process_name=None,
                            samples=SIGNATURE_SAMPLES, interval=SIGNATURE_SAMPLE_INTERVAL):
    """Assinatura barata do estado atual da tela.

    Captura `samples` vezes em sequencia e marca como VOLATIL toda celula que
    variou entre as amostras: relogio, spinner, contador de frame da Unity. Sem
    isso, qualquer hash de tela inteira daria falso negativo o tempo todo.
    """
    started = time.time()
    samples = max(1, min(10, int(samples)))
    grids = []
    window = None
    global_hash = 0
    for index in range(samples):
        image, window, _origin = _signature_source_image(
            monitor=monitor, hwnd=hwnd, window_kw=window_kw, process_name=process_name)
        if index == 0:
            global_hash = _dhash_bits(image)
        grids.append(_grid_hashes(image))
        if index < samples - 1:
            time.sleep(interval)

    cells = grids[0]
    volatile = sorted({i for grid in grids[1:] for i in range(len(cells))
                       if grid[i] != cells[i]})

    return {
        "dhash": f"{global_hash:016x}",
        "cells": [f"{c:016x}" for c in cells],
        "volatile_cells": volatile,
        "grid": [SIGNATURE_GRID, SIGNATURE_GRID],
        "monitor": str(monitor),
        "samples": samples,
        "window": {
            "hwnd": (window or {}).get("hwnd"),
            "process": (window or {}).get("process"),
            "title": (window or {}).get("title"),
            "client_size": [(window or {}).get("width"), (window or {}).get("height")],
        },
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def signature_distance(expected, current, tolerance=None):
    """Compara duas assinaturas ignorando as celulas volateis de qualquer lado.

    A identidade da janela (processo) e porteira, nao distancia: outro app em
    foco nunca deve 'quase bater'. O resto vira distancia de Hamming somada.
    """
    tolerance = SIGNATURE_TOLERANCE if tolerance is None else max(0, int(tolerance))
    exp_proc = ((expected or {}).get("window") or {}).get("process")
    cur_proc = ((current or {}).get("window") or {}).get("process")
    if exp_proc and cur_proc and exp_proc != cur_proc:
        return {"match": False, "reason": "process_changed", "distance": None,
                "expected_process": exp_proc, "current_process": cur_proc,
                "changed_cells": []}

    exp_cells = (expected or {}).get("cells") or []
    cur_cells = (current or {}).get("cells") or []
    if not exp_cells or len(exp_cells) != len(cur_cells):
        return {"match": False, "reason": "grid_mismatch", "distance": None,
                "changed_cells": []}

    ignored = set((expected or {}).get("volatile_cells") or [])
    ignored.update((current or {}).get("volatile_cells") or [])

    distance = 0
    changed = []
    for index, (a, b) in enumerate(zip(exp_cells, cur_cells)):
        if index in ignored:
            continue
        cell_distance = _hamming(int(a, 16), int(b, 16))
        if cell_distance:
            distance += cell_distance
            changed.append({"cell": index, "distance": cell_distance})

    changed.sort(key=lambda item: item["distance"], reverse=True)
    return {
        "match": distance <= tolerance,
        "reason": "ok" if distance <= tolerance else "dhash_distance",
        "distance": distance,
        "tolerance": tolerance,
        "ignored_cells": sorted(ignored),
        "changed_cells": changed[:8],
    }



# =====================================================================
# SNAPSHOT INCREMENTAL POR TAG ESTAVEL (IDEIAS/03 - item 4')
# =====================================================================
# Substitui a ideia de dirty rects. Eles eram MEIO, nao fim: o fim era parar de
# reenviar 60 marcas quando 3 mudaram. Da para fazer isso comparando as marcas,
# sem DXGI e sem sofrer do flip model - e so e possivel porque as tags do SoM
# ja sao estaveis entre capturas (assign_stable_som_tags).

SNAPSHOT_TOKEN_VERSION = "som1"
# Tolerancia de bbox para nao chamar jitter de OCR de movimento. Sem ela toda
# marca sairia como "moved" e o diff ficaria maior que o snapshot completo.
#
# Calibrado com medicao em Windows real (WinRT OCR, Unity 6.5, 385 pares de
# palavras em 10 capturas de tela parada): 99,5% das marcas ficaram com
# deslocamento EXATAMENTE zero, 100% ficaram em <= 1 px de centro e <= 1 px de
# borda. O jitter do WinRT e muito menor do que eu supunha - o ruido que existe
# e de SEGMENTACAO (palavra de fonte pequena some e volta, virando added/removed),
# nao de coordenada.
#
# 3 px deixa 2 px de folga sobre o pior caso medido e ainda pega qualquer
# arraste ou scroll real, que move >= 4 px. O valor antigo (2) tambem nao
# produzia falso positivo nesses dados; 3 e a margem, nao a correcao.
SNAPSHOT_MOVE_TOLERANCE = max(0, int(os.environ.get("SNAPSHOT_MOVE_TOLERANCE", "3")))
# Diff so vale se ficar abaixo desta fracao do payload completo.
SNAPSHOT_DIFF_MAX_RATIO = max(0.1, min(1.0, float(os.environ.get("SNAPSHOT_DIFF_MAX_RATIO", "0.8"))))
# Acima desta fracao de tags com identidade quebrada, o rastreador provavelmente
# perdeu o fio: manda completo em vez de descrever uma tela que nao se reconhece.
SNAPSHOT_MAX_IDENTITY_BREAK = max(0.05, min(1.0, float(os.environ.get("SNAPSHOT_MAX_IDENTITY_BREAK", "0.4"))))
# Completo periodico: limita por quanto tempo um erro de rastreio sobrevive.
SNAPSHOT_FULL_EVERY = max(1, int(os.environ.get("SNAPSHOT_FULL_EVERY", "10")))
# Carencia para o flicker de SEGMENTACAO do OCR: palavra de fonte pequena some e
# volta entre capturas de uma tela parada. Sem carencia isso vira removed+added
# a cada ida e volta - churn puro num diff que deveria estar vazio.
# A tolerancia de bbox nao alcanca esse caso: a marca nao se moveu, ela sumiu.
# 0 desliga a carencia e restaura o comportamento original sem retencao.
# Medicao em Windows real (Unity 6.5, Console, Inspector, 40 capturas consecutivas):
# Taxa de flicker espontaneo = 0.00% (zero gaps em 3.440 amostras).
# Portanto, fica desativado por padrao (0) e disponivel como opcao defensiva (ex: N=2).
SNAPSHOT_QUARANTINE_CAPTURES = max(0, int(os.environ.get("SNAPSHOT_QUARANTINE_CAPTURES", "0")))

# Campos que, mudando, tornam a marca "alterada" e nao "igual".
_CAMPOS_DE_CONTEUDO = ("text", "type", "bg", "fg", "confidence")

_snapshot_store = {}
_snapshot_store_lock = threading.RLock()


def _hash_conteudo(marks):
    material = "|".join(
        f"{m.get('tag')}:{m.get('type')}:{m.get('text')}:{m.get('bbox')}"
        for m in sorted(marks, key=lambda m: m.get("tag") or 0))
    return hashlib.sha1(material.encode("utf-8", "replace")).hexdigest()[:8]


def build_snapshot_token(frame_id, monitor, hwnd, marks):
    """Token opaco para o cliente, legível para o servidor."""
    return ".".join((
        SNAPSHOT_TOKEN_VERSION,
        str(frame_id or "")[:12],
        str(monitor),
        str(int(hwnd or 0)),
        str(len(marks)),
        _hash_conteudo(marks),
    ))


def parse_snapshot_token(token):
    """Decompõe o token. Devolve None para qualquer coisa que não seja `som1`."""
    if not token or not isinstance(token, str):
        return None
    partes = token.split(".")
    if len(partes) != 6 or partes[0] != SNAPSHOT_TOKEN_VERSION:
        return None
    try:
        return {"frame_id": partes[1], "monitor": partes[2],
                "hwnd": int(partes[3]), "count": int(partes[4]), "hash": partes[5]}
    except ValueError:
        return None


def _identidade_quebrada(anterior, atual):
    """A tag e a mesma, mas o elemento e claramente outro?

    Existe porque o rastreador REUSA a tag quando acha que e o mesmo elemento.
    Se ele errar, dizer "tag 7 mudou de texto" faria o cliente acreditar que
    continua sendo o mesmo controle. Nesses casos o diff quebra a identidade de
    proposito - sai como removida e adicionada -, em vez de redirecionar em
    silencio.
    """
    if anterior.get("type") != atual.get("type"):
        return True
    a_txt = str(anterior.get("text") or "").strip().lower()
    b_txt = str(atual.get("text") or "").strip().lower()
    if a_txt and b_txt and a_txt != b_txt and a_txt not in b_txt and b_txt not in a_txt:
        return True
    ax, ay, _aw, ah = anterior.get("bbox") or (0, 0, 0, 0)
    bx, by, _bw, _bh = atual.get("bbox") or (0, 0, 0, 0)
    limite = max(12, (ah or 12) * 3)
    if abs(ax - bx) > limite or abs(ay - by) > limite:
        return True
    return False


def diff_marks(anteriores, atuais):
    """Compara duas listas de marcas POR TAG, nunca por posição na lista."""
    por_tag_antes = {m.get("tag"): m for m in anteriores if m.get("tag") is not None}
    por_tag_agora = {m.get("tag"): m for m in atuais if m.get("tag") is not None}

    added, removed, moved, changed = [], [], [], []
    iguais = 0
    quebras = 0

    for tag, atual in por_tag_agora.items():
        anterior = por_tag_antes.get(tag)
        if anterior is None:
            added.append(atual)
            continue
        if _identidade_quebrada(anterior, atual):
            # Nao redirecionar: some a antiga e nasce a nova, com a mesma tag.
            quebras += 1
            removed.append(tag)
            added.append(atual)
            continue

        delta = {}
        for campo in _CAMPOS_DE_CONTEUDO:
            if anterior.get(campo) != atual.get(campo):
                delta[campo] = atual.get(campo)
        ax, ay, aw, ah = anterior.get("bbox") or (0, 0, 0, 0)
        bx, by, bw, bh = atual.get("bbox") or (0, 0, 0, 0)
        mexeu = (abs(ax - bx) > SNAPSHOT_MOVE_TOLERANCE or
                 abs(ay - by) > SNAPSHOT_MOVE_TOLERANCE or
                 abs(aw - bw) > SNAPSHOT_MOVE_TOLERANCE or
                 abs(ah - bh) > SNAPSHOT_MOVE_TOLERANCE)
        if mexeu:
            moved.append({"tag": tag, "bbox": atual.get("bbox"),
                          "center": atual.get("center")})
        if delta:
            delta["tag"] = tag
            changed.append(delta)
        if not mexeu and not delta:
            iguais += 1

    for tag in por_tag_antes:
        if tag not in por_tag_agora:
            removed.append(tag)

    total = max(1, len(por_tag_agora))
    return {"added": added, "removed": sorted(set(removed)),
            "moved": moved, "changed": changed, "unchanged": iguais,
            "identity_breaks": quebras,
            "identity_break_ratio": round(quebras / total, 3)}


def _reaparecimento_compativel(antes, agora):
    """A marca que voltou e mesmo a que sumiu?

    Reusa `_identidade_quebrada`, que ja e a regra auditada para "a tag e a
    mesma mas o elemento e outro". Se um elemento DIFERENTE nascer na posicao da
    marca em carencia e herdar a tag do rastreador, isto pega - e o resgate vira
    removed + added, como seria sem carencia.
    """
    return not _identidade_quebrada(antes, agora)


def _atualizar_quarentena(anteriores, quarentena_antes, atuais, limite):
    """Segura por `limite` capturas quem sumiu, antes de declarar removed.

    Devolve (quarentena, resgatadas, expiradas, incompativeis). A quarentena
    guarda IDENTIDADE, nao permissao de clique: quem esta aqui nao aparece em
    `marks` e nao e clicavel - so nao e anunciado como removido ainda.
    """
    por_tag_agora = {m.get("tag"): m for m in atuais if m.get("tag") is not None}
    quarentena_antes = quarentena_antes or {}
    quarentena = {}
    resgatadas, expiradas, incompativeis = [], [], []
    agora = time.time()

    for tag, item in quarentena_antes.items():
        atual = por_tag_agora.get(tag)
        if atual is not None:
            (resgatadas if _reaparecimento_compativel(item["mark"], atual)
             else incompativeis).append(tag)
            continue
        faltas = int(item.get("faltas", 0)) + 1
        if faltas > limite:
            expiradas.append(tag)
        else:
            quarentena[tag] = {"mark": item["mark"], "faltas": faltas,
                               "desde": item.get("desde", agora)}

    for marca in anteriores:
        tag = marca.get("tag")
        if tag is None or tag in por_tag_agora or tag in quarentena_antes:
            continue
        if limite <= 0:
            expiradas.append(tag)
        else:
            quarentena[tag] = {"mark": dict(marca), "faltas": 1, "desde": agora}

    return quarentena, resgatadas, sorted(set(expiradas)), sorted(set(incompativeis))


def snapshot_store_put(monitor, hwnd, token, marks, truncated=False):
    """Guarda APENAS o último snapshot de cada (monitor, janela)."""
    with _snapshot_store_lock:
        chave = (str(monitor), int(hwnd or 0))
        anterior = _snapshot_store.get(chave) or {}
        quarentena, resgatadas, expiradas, incompativeis = _atualizar_quarentena(
            anterior.get("marks") or [], anterior.get("quarentena"), marks,
            SNAPSHOT_QUARANTINE_CAPTURES)
        _snapshot_store[chave] = {
            "token": token,
            "marks": [dict(m) for m in marks],
            "at": time.time(),
            "truncated": bool(truncated),
            "diffs_seguidos": anterior.get("diffs_seguidos", 0),
            "quarentena": quarentena,
            "resgatadas": resgatadas,
            "expiradas": expiradas,
            "incompativeis": incompativeis,
        }
        return _snapshot_store[chave]


def snapshot_store_get(monitor, hwnd):
    with _snapshot_store_lock:
        item = _snapshot_store.get((str(monitor), int(hwnd or 0)))
        return dict(item) if item else None


def marca_em_quarentena(monitor, hwnd, tag):
    """Devolve o registro da tag em carência, ou None."""
    with _snapshot_store_lock:
        item = _snapshot_store.get((str(monitor), int(hwnd or 0))) or {}
        achado = (item.get("quarentena") or {}).get(int(tag))
        return dict(achado) if achado else None


def snapshot_store_marcar_diff(monitor, hwnd, reiniciar=False):
    with _snapshot_store_lock:
        item = _snapshot_store.get((str(monitor), int(hwnd or 0)))
        if not item:
            return 0
        item["diffs_seguidos"] = 0 if reiniciar else item.get("diffs_seguidos", 0) + 1
        return item["diffs_seguidos"]



def decidir_resposta_som(marks, monitor, hwnd, frame_id, since, truncado=False):
    """Decide entre diff e snapshot completo, e monta a resposta.

    Sete motivos mandam completo. Em nenhum deles o cliente ve erro: token
    velho DEGRADA a leitura, nunca a recusa. Quem recusa e o frame_id, do lado
    da escrita - sao mecanismos ortogonais e misturar os dois seria o erro.
    """
    token_novo = build_snapshot_token(frame_id, monitor, hwnd, marks)
    completo = {"kind": "full", "snapshotToken": token_novo}

    def mandar_completo(motivo):
        estado = snapshot_store_put(monitor, hwnd, token_novo, marks, truncado)
        snapshot_store_marcar_diff(monitor, hwnd, reiniciar=True)
        saida = dict(completo)
        if motivo:
            saida["fullReason"] = motivo
        segurados = sorted(estado.get("quarentena") or {})
        if segurados:
            # Nao entram em `marks`: o cliente ve que a tag existe e nao esta
            # clicavel agora, em vez de descobrir isso com um clique no vazio.
            saida["quarantined"] = segurados
        return saida

    if truncado:
        # Diff sobre coleta incompleta descreveria remocoes que nao houve.
        return mandar_completo("captura_truncada")

    pedido = parse_snapshot_token(since)
    if since and not pedido:
        return mandar_completo("token_invalido")
    if not pedido:
        return mandar_completo(None)          # primeiro acesso: sem motivo a relatar
    if pedido["monitor"] != str(monitor) or pedido["hwnd"] != int(hwnd or 0):
        return mandar_completo("janela_ou_monitor_diferente")

    anterior = snapshot_store_get(monitor, hwnd)
    if not anterior:
        return mandar_completo("sem_snapshot_anterior")
    if anterior.get("token") != since:
        return mandar_completo("token_superado")
    if anterior.get("truncated"):
        return mandar_completo("snapshot_anterior_truncado")
    idade = time.time() - float(anterior.get("at") or 0)
    if idade > SNAPSHOT_TTL_SECONDS:
        return mandar_completo("token_expirado")
    if anterior.get("diffs_seguidos", 0) >= SNAPSHOT_FULL_EVERY:
        # Completo periodico: limita por quanto tempo um erro de rastreio vive.
        return mandar_completo("completo_periodico")

    # A base do diff inclui quem esta em carencia: assim a marca que volta sai
    # como unchanged/changed em vez de added, e o `removed` que ela geraria ao
    # sumir nem chegou a ser emitido.
    quarentena = anterior.get("quarentena") or {}
    base = list(anterior.get("marks") or []) + [q["mark"] for q in quarentena.values()]
    diff = diff_marks(base, marks)
    if diff["identity_break_ratio"] > SNAPSHOT_MAX_IDENTITY_BREAK:
        return mandar_completo("identidade_instavel")

    if SNAPSHOT_QUARANTINE_CAPTURES > 0:
        # Quem sumiu agora entra em carencia e sai do `removed`. Quem ja estava
        # e esgotou a carencia e que vira removed de verdade - uma vez so.
        futura, _resg, expiradas, _incomp = _atualizar_quarentena(
            anterior.get("marks") or [], quarentena, marks,
            SNAPSHOT_QUARANTINE_CAPTURES)
        segurados = set(futura)
        diff["removed"] = [t for t in diff["removed"] if t not in segurados]
        for tag in expiradas:
            if tag not in diff["removed"]:
                diff["removed"].append(tag)
        diff["removed"] = sorted(set(diff["removed"]))
        # Identidade preservada, clique NAO: quem esta aqui nao esta em `marks`.
        diff["quarantined"] = sorted(segurados)

    # Diff so compensa se for menor que o completo. Tela que muda inteira gera
    # removed de 60 tags MAIS added de 60 marcas cheias - maior que o completo.
    bytes_diff = len(json.dumps(diff, ensure_ascii=False))
    bytes_full = len(json.dumps(marks, ensure_ascii=False))
    if bytes_diff >= bytes_full * SNAPSHOT_DIFF_MAX_RATIO:
        return mandar_completo("diff_nao_compensa")

    snapshot_store_put(monitor, hwnd, token_novo, marks, truncado)
    snapshot_store_marcar_diff(monitor, hwnd)
    return {"kind": "diff", "snapshotToken": token_novo,
            "baseSnapshotToken": since, "savedRatio": round(1 - bytes_diff / max(1, bytes_full), 3),
            **diff}


def publish_state_snapshot(mode, monitor, *, elements=None, marks=None, last_som_image=None):
    """Publica um snapshot completo de maneira atômica e devolve uma cópia rasa."""
    dxgi_watermark = None
    oracle = get_dxgi_oracle()
    if oracle:
        dxgi_watermark = oracle.get_watermark()

    # `list()` perde o atributo .truncation da _ListaComCota, e sem ele o cache
    # esquece que a coleta foi parcial - um cache-hit depois reserviria 300 de
    # 800 marcas como se fossem a tela inteira.
    truncation = (getattr(marks, "truncation", None)
                  or getattr(elements, "truncation", None))
    snapshot = {
        "mode": mode,
        "timestamp": time.time(),
        "frame_id": uuid.uuid4().hex,
        "monitor": str(monitor),
        "window": get_foreground_window_info(),
        "elements": list(elements or []),
        "marks": list(marks or []),
        "last_som_image": last_som_image,
        "dxgi_watermark": dxgi_watermark,
        "truncation": truncation,
    }
    with state_cache_lock:
        current_state_cache.clear()
        current_state_cache.update(snapshot)
    return dict(snapshot)


def read_state_snapshot():
    """Lê metadados e coleções do cache sem observar uma atualização parcial."""
    with state_cache_lock:
        snapshot = dict(current_state_cache)
        snapshot["elements"] = list(current_state_cache.get("elements", []))
        snapshot["marks"] = list(current_state_cache.get("marks", []))
        return snapshot


def validate_frame_reference(body, snapshot):
    """Impede que clientes novos usem tags/IDs obtidos de uma captura anterior."""
    requested = body.get("frame_id")
    if requested and requested != snapshot.get("frame_id"):
        return {
            "ok": False,
            "error_code": "stale_frame",
            "error": "O frame informado não é mais o frame ativo. Capture /state novamente.",
            "requested_frame_id": requested,
            "current_frame_id": snapshot.get("frame_id"),
        }

    if requested:
        age = max(0.0, time.time() - float(snapshot.get("timestamp") or 0))
        if age > SNAPSHOT_TTL_SECONDS:
            return {
                "ok": False,
                "error_code": "expired_frame",
                "error": "O frame informado expirou. Capture /state novamente.",
                "frame_id": snapshot.get("frame_id"),
                "age_seconds": round(age, 3),
                "ttl_seconds": SNAPSHOT_TTL_SECONDS,
            }

        if body.get("verify_window", True):
            expected_window = snapshot.get("window") or {}
            current_window = get_foreground_window_info() or {}
            expected_hwnd = int(expected_window.get("hwnd") or 0)
            current_hwnd = int(current_window.get("hwnd") or 0)
            if expected_hwnd and current_hwnd != expected_hwnd:
                return {
                    "ok": False,
                    "error_code": "window_changed",
                    "error": "A janela em primeiro plano mudou desde a captura. Capture /state novamente.",
                    "frame_id": snapshot.get("frame_id"),
                    "expected_window": expected_window,
                    "current_window": current_window,
                }
    return None


# =====================================================================
# COTA E AVISO DE TRUNCAMENTO
# =====================================================================
# Cortar a coleta em silencio e pior do que devolver menos: o agente recebe 80
# elementos de uma grade de 3000, acredita que e a janela inteira e conclui
# "o item nao existe" quando ele esta na linha 500. Resposta errada com cara de
# resposta certa. Toda coleta com teto passa a dizer que bateu no teto.

UIA_MAX_ELEMENTS = max(10, int(os.environ.get("UIA_MAX_ELEMENTS", "80")))
UIA_MAX_DEPTH = max(1, int(os.environ.get("UIA_MAX_DEPTH", "4")))
UIA_TIMEOUT_MS = max(200, int(os.environ.get("UIA_TIMEOUT_MS", "2000")))
SOM_MAX_MARKS = max(20, int(os.environ.get("SOM_MAX_MARKS", "300")))


class _ListaComCota(list):
    """Lista que carrega o aviso de truncamento sem quebrar quem so itera.

    E uma lista para todos os efeitos - len, iteracao, json.dumps, indexacao -,
    entao os pontos de chamada existentes nao mudam.

    ARMADILHA: copiar (list(x), x[:], sorted(x)) PERDE o atributo, porque a
    copia e uma list comum. Quem precisa do aviso tem que ler `.truncation`
    ANTES de copiar. publish_state_snapshot copia, entao get_system_state le
    antes de passar adiante.
    """

    truncation = None


def _aviso_truncamento(motivo, coletados, limite, extra=None):
    aviso = {"truncated": True, "reason": motivo,
             "collected": coletados, "limit": limite}
    if extra:
        aviso.update(extra)
    return aviso


def inspect_window_uia(hwnd=None, title_kw=None, max_elements=None,
                       max_depth=None, timeout_ms=None):
    """Extrai árvore semântica da janela em foco (100% determinística para apps convencionais).

    Devolve uma `_ListaComCota`: se a coleta parou por teto de elementos, de
    profundidade ou de tempo, `.truncation` diz qual e quantos foram coletados.
    """
    ensure_desktop_access()
    limite = UIA_MAX_ELEMENTS if max_elements is None else max(1, int(max_elements))
    profundidade = UIA_MAX_DEPTH if max_depth is None else max(1, int(max_depth))
    prazo_ms = UIA_TIMEOUT_MS if timeout_ms is None else max(100, int(timeout_ms))

    target_hwnd = hwnd
    if not target_hwnd and title_kw:
        ok, target_win = focus_window(title_kw)
        if ok and target_win:
            target_hwnd = target_win.get("hwnd")
    if not target_hwnd:
        target_hwnd = user32.GetForegroundWindow()

    saida = _ListaComCota()
    if not target_hwnd or not uia:
        return saida

    # A thread worker publica de uma vez no fim. Antes ela dava append numa
    # lista que o chamador ja tinha retornado: com o join estourando, duas
    # threads mexiam na mesma lista e o JSON saia inconsistente.
    publicado = {"elements": [], "truncation": None}
    parar = threading.Event()
    prazo = time.monotonic() + prazo_ms / 1000.0

    def _worker():
        locais = []
        motivo = None
        try:
            ensure_desktop_access()
            with uia.UIAutomationInitializerInThread():
                ctrl = uia.ControlFromHandle(target_hwnd)
                if not ctrl:
                    return
                elem_id = 1

                def walk(node, depth=0):
                    nonlocal elem_id, motivo
                    if motivo:
                        return
                    if len(locais) >= limite:
                        motivo = "limite_de_elementos"
                        return
                    if parar.is_set() or time.monotonic() > prazo:
                        motivo = "tempo_esgotado"
                        return
                    if depth > profundidade:
                        # Profundidade e truncamento tambem: a arvore continua
                        # abaixo, e o agente precisa saber que nao olhamos.
                        motivo = motivo or "limite_de_profundidade"
                        return
                    for child in node.GetChildren():
                        if motivo:
                            return
                        t = child.ControlTypeName
                        n = child.Name or ""
                        r = child.BoundingRectangle
                        if r.width() > 5 and r.height() > 5:
                            is_interactive = t in [
                                'ButtonControl', 'EditControl', 'MenuItemControl',
                                'TabItemControl', 'CheckBoxControl', 'RadioButtonControl',
                                'HyperlinkControl', 'SplitButtonControl', 'TreeItemControl',
                                'ListItemControl', 'ComboBoxControl'
                            ] or (n and len(n.strip()) > 0 and t not in ['PaneControl', 'WindowControl'])

                            if is_interactive:
                                if len(locais) >= limite:
                                    motivo = "limite_de_elementos"
                                    return
                                cx = (r.left + r.right) // 2
                                cy = (r.top + r.bottom) // 2
                                locais.append({
                                    "id": elem_id,
                                    "name": n.strip(),
                                    "type": t,
                                    "center": [cx, cy],
                                    "bbox": [r.left, r.top, r.width(), r.height()]
                                })
                                elem_id += 1
                        walk(child, depth + 1)

                walk(ctrl)
        except Exception as exc:
            motivo = motivo or f"erro: {exc}"
        finally:
            # Rebind de chave de dict e atomico sob o GIL: o chamador ou ve a
            # lista vazia inicial, ou a lista pronta. Nunca uma pela metade.
            publicado["elements"] = locais
            if motivo:
                publicado["truncation"] = _aviso_truncamento(
                    motivo, len(locais), limite,
                    {"max_depth": profundidade, "timeout_ms": prazo_ms})

    # daemon: se a thread ficar presa numa chamada COM que nao da para
    # interromper, ela nao impede o processo de encerrar.
    t = threading.Thread(target=_worker, daemon=True, name="uia-walk")
    t.start()
    t.join(timeout=prazo_ms / 1000.0 + 0.25)
    if t.is_alive():
        parar.set()
        t.join(timeout=0.5)

    saida.extend(publicado["elements"])
    if t.is_alive():
        saida.truncation = _aviso_truncamento(
            "thread_travada", len(saida), limite,
            {"detail": "a varredura UIA nao respondeu ao pedido de parada"})
    else:
        saida.truncation = publicado["truncation"]
    return saida

def _box_iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0, min(ay2, by2) - max(ay1, by1))
    intersection = iw * ih
    union = max(1, (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - intersection)
    return intersection / union


def _som_xyxy(mark):
    x, y, width, height = mark["bbox"]
    return (x, y, x + width, y + height)


def _som_normalized_text(mark):
    return " ".join(str(mark.get("text", "")).casefold().split())


def _load_som_trackers_locked():
    global som_trackers_loaded
    if som_trackers_loaded:
        return
    som_trackers_loaded = True
    try:
        with open(SOM_TRACK_STATE_PATH, "r", encoding="utf-8") as state_file:
            stored = json.load(state_file)
        for monitor, tracker in stored.get("monitors", {}).items():
            tracks = {}
            for tag, track in tracker.get("tracks", {}).items():
                numeric_tag = int(tag)
                tracks[numeric_tag] = {
                    "type": track.get("type"),
                    "normalized_text": track.get("normalized_text", ""),
                    "center": list(track.get("center", [0, 0])),
                    "box": tuple(track.get("box", [0, 0, 0, 0])),
                    "last_seen": int(track.get("last_seen", 0)),
                }
            som_trackers[str(monitor)] = {
                "next_tag": max(int(tracker.get("next_tag", 1)), max(tracks, default=0) + 1),
                "generation": int(tracker.get("generation", 0)), "tracks": tracks,
            }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def _save_som_trackers_locked(force=False):
    """Grava o estado das tags no disco, no máximo a cada SOM_TRACK_SAVE_INTERVAL.

    Antes a gravacao (com fsync de dezenas de KB) acontecia a cada captura SoM, e
    click_and_verify/wait_text chamam captura em laco: era latencia e desgaste de
    disco por nada, ja que o arquivo so precisa sobreviver ao fim do processo.
    """
    global som_trackers_last_save
    now = time.time()
    if not force and (now - som_trackers_last_save) < SOM_TRACK_SAVE_INTERVAL:
        return
    som_trackers_last_save = now
    directory = os.path.dirname(os.path.abspath(SOM_TRACK_STATE_PATH))
    temporary_path = SOM_TRACK_STATE_PATH + ".tmp"
    payload = {"version": 1, "monitors": som_trackers}
    try:
        os.makedirs(directory, exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, ensure_ascii=False, separators=(",", ":"))
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, SOM_TRACK_STATE_PATH)
    except OSError:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass


def flush_som_trackers():
    """Força a gravação pendente do estado das tags ao encerrar o processo."""
    with som_tracker_lock:
        if som_trackers_loaded:
            _save_som_trackers_locked(force=True)


atexit.register(flush_som_trackers)


def assign_stable_som_tags(marks, monitor="1"):
    """Mantém a tag do mesmo elemento entre capturas e nunca recicla IDs."""
    monitor_key = str(monitor)
    with som_tracker_lock:
        _load_som_trackers_locked()
        tracker = som_trackers.setdefault(monitor_key, {
            "next_tag": 1, "generation": 0, "tracks": {}
        })
        tracker["generation"] += 1
        generation = tracker["generation"]
        tracks = tracker["tracks"]
        unmatched_tracks = set(tracks)
        candidates = []
        for mark_index, mark in enumerate(marks):
            mx, my = mark["center"]
            mark_type = mark.get("type")
            normalized_text = _som_normalized_text(mark)
            for tag, track in tracks.items():
                if track["type"] != mark_type:
                    continue
                tx, ty = track["center"]
                distance = ((mx - tx) ** 2 + (my - ty) ** 2) ** 0.5
                iou = _box_iou(_som_xyxy(mark), track["box"])
                same_text = track["normalized_text"] == normalized_text
                # OCR pode ler o mesmo controle de forma um pouco diferente. Uma
                # sobreposicao forte preserva a identidade mesmo se o texto mudar.
                if mark_type == "text" and not same_text and iou < 0.45:
                    continue
                if distance > SOM_TRACK_MAX_DISTANCE and iou < 0.15:
                    continue
                text_bonus = 2.0 if mark_type == "text" and same_text else 0.0
                score = text_bonus + (iou * 4.0) + max(0.0, 1.0 - distance / SOM_TRACK_MAX_DISTANCE)
                candidates.append((score, -distance, mark_index, tag))

        assigned_marks = set()
        for _, _, mark_index, tag in sorted(candidates, reverse=True):
            if mark_index in assigned_marks or tag not in unmatched_tracks:
                continue
            marks[mark_index]["tag"] = tag
            marks[mark_index]["tracking"] = "matched"
            assigned_marks.add(mark_index)
            unmatched_tracks.remove(tag)

        for mark_index, mark in enumerate(marks):
            if mark_index not in assigned_marks:
                tag = tracker["next_tag"]
                tracker["next_tag"] += 1
                mark["tag"] = tag
                mark["tracking"] = "new"
            tag = mark["tag"]
            tracks[tag] = {
                "type": mark.get("type"),
                "normalized_text": _som_normalized_text(mark),
                "center": list(mark["center"]),
                "box": _som_xyxy(mark),
                "last_seen": generation,
            }

        expired = [tag for tag, track in tracks.items()
                   if generation - track["last_seen"] > SOM_TRACK_MAX_MISSES]
        for tag in expired:
            del tracks[tag]

        # A expiracao acima depende da geracao AVANCAR naquele monitor: um monitor
        # que parou de ser capturado congela e seus tracks nunca saem. Sem teto o
        # arquivo de estado so cresce e fica cada vez mais lento para carregar.
        excess = len(tracks) - SOM_TRACK_MAX_TRACKS
        if excess > 0:
            for tag in sorted(tracks, key=lambda t: tracks[t]["last_seen"])[:excess]:
                del tracks[tag]

        _save_som_trackers_locked()
        return marks

def _segment_ocr_words(words, scale=1.0):
    """Divide uma linha OCR em grupos visuais, evitando unir botoes vizinhos."""
    parsed = []
    for word in words:
        rect = word.get("bounding_rect", {})
        text = " ".join(str(word.get("text", "")).split())
        try:
            x, y = float(rect["x"]), float(rect["y"])
            width, height = float(rect["width"]), float(rect["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if text and width > 0 and height > 0:
            parsed.append({"text": text, "x": x, "y": y,
                           "width": width, "height": height})
    parsed.sort(key=lambda item: item["x"])
    if not parsed:
        return []

    factor = max(0.8, float(os.environ.get("SOM_OCR_GROUP_GAP_FACTOR", "1.35")))
    groups = [[parsed[0]]]
    for word in parsed[1:]:
        previous = groups[-1][-1]
        gap = word["x"] - (previous["x"] + previous["width"])
        previous_char = previous["width"] / max(1, len(previous["text"]))
        current_char = word["width"] / max(1, len(word["text"]))
        typical_char = (previous_char + current_char) / 2.0
        typical_height = (previous["height"] + word["height"]) / 2.0
        split_gap = max(typical_char * 2.8, typical_height * factor, 10.0 * scale)
        if gap > split_gap:
            groups.append([word])
        else:
            groups[-1].append(word)
    return groups


def _ocr_candidates(image, lang="pt-BR", scale=1.0, source="original"):
    if not winocr:
        return []
    result = winocr.recognize_pil_sync(image, lang=lang)
    candidates = []
    for line in result.get("lines", []):
        words = line.get("words", [])
        for group in _segment_ocr_words(words, scale=scale):
            text = " ".join(word["text"] for word in group)
            x1 = round(min(word["x"] for word in group) / scale)
            y1 = round(min(word["y"] for word in group) / scale)
            x2 = round(max(word["x"] + word["width"] for word in group) / scale)
            y2 = round(max(word["y"] + word["height"] for word in group) / scale)
            if x2 > x1 and y2 > y1:
                candidates.append({"text": text, "box": (x1, y1, x2, y2), "source": source})
    return candidates

def _merge_ocr_candidates(candidates):
    merged = []
    for candidate in sorted(candidates, key=lambda item: (item["source"] != "original", -len(item["text"]))):
        normalized = candidate["text"].casefold().strip()
        duplicate_index = None
        for index, existing in enumerate(merged):
            same_text = normalized == existing["text"].casefold().strip()
            overlap = _box_iou(candidate["box"], existing["box"])
            if overlap >= 0.55 or (same_text and overlap >= 0.25):
                duplicate_index = index
                break
        if duplicate_index is None:
            merged.append(candidate)
        elif len(candidate["text"]) > len(merged[duplicate_index]["text"]):
            merged[duplicate_index] = candidate
    return sorted(merged, key=lambda item: (item["box"][1] // 12, item["box"][0]))

def _enhanced_ocr_images(image, aggressive=False):
    if cv2 is None or np is None:
        return []
    rgb = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(gray)
    scale = min(2.0, max(1.35, 2600.0 / max(1, max(image.size))))
    enhanced = cv2.resize(clahe, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    variants = [(Image.fromarray(enhanced).convert("RGB"), scale, "clahe")]
    if aggressive:
        block_size = max(15, int(round(31 * scale)) | 1)
        binary = cv2.adaptiveThreshold(enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, block_size, 11)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        variants.append((Image.fromarray(binary).convert("RGB"), scale, "adaptive"))
        variants.append((Image.fromarray(255 - binary).convert("RGB"), scale, "adaptive-inverted"))
    return variants

# Detector de icones v2 (opt-in: SOM_ICON_DETECTOR=v2). Medido no Unity: 24
# de 32 icones perdidos pelo v1 estavam GRUDADOS nas bordas dos paineis - o
# editor inteiro virava um componente de 1360x668 e era descartado por
# tamanho - e 8 eram o menu "⋮", tres pontos de 4 px de largura. O v2 tira as
# linhas longas antes de separar componentes e aceita o formato do "⋮".
#
# Padrao "auto" (acordado com a revisao externa depois de 12 holdouts do Unity,
# 614 pontos): v2 quando o monitor capturado mostra o Unity, v1 no resto.
# v1 -> v2 no Unity: recall de icone 28,0% -> 69,7%, precisao 61,8% -> 82,5%,
# duplicatas 78 -> 0, bytes -4,6%, +18 ms. As metas originais (precisao >= 90%,
# lixo <= 10%) NAO foram atingidas: 82,5%/17,5% e o teto observado desta
# familia de heuristicas, registrado como tal. Fora do Unity o v2 nao foi
# medido, por isso nao vale la. SOM_ICON_DETECTOR=v1|v2 forca um dos dois.
SOM_ICON_DETECTOR = os.environ.get("SOM_ICON_DETECTOR", "auto").strip().lower()
APPS_DETECTOR_V2 = {"unity.exe"}


def detector_de_icone_para(monitor_rect):
    """'v2' ou 'v1' para uma captura deste monitor."""
    if SOM_ICON_DETECTOR in ("v1", "v2"):
        return SOM_ICON_DETECTOR
    ox, oy, mw, mh = monitor_rect
    fg = get_foreground_window_info()
    if str(fg.get("process") or "").lower() in APPS_DETECTOR_V2:
        # Em foco nao basta: com o Unity em foco no monitor 2, uma captura do
        # monitor 1 mostra outra coisa. Precisa cobrir boa parte deste monitor.
        l, t = max(ox, fg["left"]), max(oy, fg["top"])
        r, b = min(ox + mw, fg["left"] + fg["width"]), min(oy + mh, fg["top"] + fg["height"])
        if r > l and b > t and (r - l) * (b - t) >= 0.25 * mw * mh:
            return "v2"
    h = user32.WindowFromPoint(wintypes.POINT(int(ox + mw // 2), int(oy + mh // 2)))
    raiz = int(user32.GetAncestor(h, GA_ROOT) or h or 0) if h else 0
    if raiz and str((describe_window(raiz) or {}).get("process") or "").lower() in APPS_DETECTOR_V2:
        return "v2"
    return "v1"
SOM_ICON_V2_LINE_PX = max(20, int(os.environ.get("SOM_ICON_V2_LINE_PX", "40")))
SOM_ICON_V2_MIN_CONFIDENCE = max(0.5, min(1.0, float(os.environ.get("SOM_ICON_V2_MIN_CONFIDENCE", "0.75"))))
# Candidatos descartados so saem no /state em modo de depuracao.
SOM_ICON_DEBUG = os.environ.get("SOM_ICON_DEBUG", "0").strip().lower() in ("1", "true", "yes")


def _remover_linhas_longas(bordas, comprimento):
    """Apaga das bordas as retas horizontais e verticais >= comprimento px.

    Sao as bordas de painel e de barra de ferramentas: ligam icones vizinhos
    num componente so. Icone de UI nao tem traco reto desse tamanho.
    """
    hz = cv2.morphologyEx(bordas, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (comprimento, 1)))
    vt = cv2.morphologyEx(bordas, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (1, comprimento)))
    return cv2.bitwise_and(bordas, cv2.bitwise_not(cv2.bitwise_or(hz, vt)))


def retangulos_de_menus_nativos():
    """Retângulos de tela dos menus Win32 (#32768) abertos agora.

    Menu nativo e texto: a seta de submenu e a marca de selecao fazem parte da
    linha do item. Medido no holdout: 38 de 74 caixas de lixo do v2 eram setas
    de submenu. Acordado: dentro desses retangulos nao se procura icone.
    """
    rects = []

    def _enum(h, _lparam):
        try:
            if user32.IsWindowVisible(h) and _classe_da_janela(h) == MENU_WINDOW_CLASS:
                r = RECT()
                if user32.GetWindowRect(h, byref(r)):
                    rects.append((r.left, r.top, r.right, r.bottom))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_enum), 0)
    except Exception:
        return []
    return rects


def _parece_tres_pontos(gray, x, y, w, h, dpi_scale=1.0):
    """O "⋮": três pontos pequenos, separados e alinhados na vertical.

    Substitui a regra antiga de "componente estreito", que aceitava seta de
    submenu (5x9), linha de arvore (3x12) e canto de aba (6x6). Aqui a forma e
    testada nos pixels, nao so no tamanho da caixa.
    """
    pad = 1
    patch = gray[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad].astype(np.int16)
    if patch.size == 0:
        return False
    fundo = float(np.median(patch))
    mascara = (np.abs(patch - fundo) > 30).astype(np.uint8)
    n, _, st, centros = cv2.connectedComponentsWithStats(mascara, connectivity=8)
    pontos = [i for i in range(1, n) if st[i][4] >= 1]
    if len(pontos) != 3:
        return False
    lado_max = max(3, round(4 * dpi_scale))
    if any(st[i][2] > lado_max or st[i][3] > lado_max for i in pontos):
        return False
    xs = [centros[i][0] for i in pontos]
    ys = sorted(centros[i][1] for i in pontos)
    if max(xs) - min(xs) > 2.5:
        return False
    vaos = (ys[1] - ys[0], ys[2] - ys[1])
    return min(vaos) >= 2 and max(vaos) / max(0.1, min(vaos)) <= 1.6


def compactar_marca_de_icone(m):
    """Forma enviada no /state para ícone no v2 (acordado: <= 120 bytes).

    Fica o que o agente usa: tag, tipo, caixa, centro e o fundo - que e o que
    mostra ferramenta selecionada. "[Icon N]" e derivavel da tag, confidence
    1.0 e o padrao, e fg/contraste/tracking nao servem para mirar em icone.
    """
    if m.get("type") not in ("icon", "icon_candidate"):
        return m
    c = {"tag": m.get("tag"), "type": m.get("type"), "bbox": m.get("bbox"), "center": m.get("center")}
    if m.get("bg"):
        c["bg"] = m["bg"]
    if (m.get("confidence") or 1.0) < 1.0:
        c["confidence"] = m["confidence"]
    for k in ("group", "row"):
        if k in m:
            c[k] = m[k]
    return c


def _filtro_contextual_v2(icon_boxes, text_boxes, mascara, imagem, motivos=None):
    """Filtros contextuais do v2 depois da detecção. Hoje: nenhum ativo.

    Tentado e retirado, com a medicao que decidiu (registro para nao repetir):
    - Icone de componente a esquerda do rotulo: calibrado, mas no holdout
      G/H/I removeu 0 lixo e derrubou 1 acerto. Retirado por acordo.
    - Letra de eixo X/Y/Z solta: o WinOCR nao le letra isolada (24 variacoes
      de recorte/escala/idioma, zero leituras), e so geometria apagaria
      play/pause/cadeado, do mesmo tamanho.
    - Barra de rolagem: mesma assinatura das alcas de slider (vazada, encostada
      no trilho); o slider de zoom e controle real.
    - Caixa que funde dois botoes: e moldura de botao (icone + borda +
      separador), sem vao vazio para cortar.

    Ativo: miniatura + rotulo logo abaixo = um elemento so (rodada 4).
    """
    return _suprimir_miniaturas(icon_boxes, text_boxes, motivos)


def _suprimir_miniaturas(icon_boxes, text_boxes, motivos=None):
    """Numa GRADE de miniaturas rotuladas, o rótulo representa o asset.

    Medido no holdout G/H/I: as 7 miniaturas do Project se partiam em varios
    "icones" (reflexo da esfera, engrenagem, chaves do Readme) - lixo que
    derrubou a precisao para 62-64% nessas telas. Pela prioridade acordada
    (texto > icone), os fragmentos DENTRO do miolo da miniatura saem.

    So vale em grade: >= 3 rotulos na mesma linha com espacamento regular.
    Um desenho sobre um rotulo avulso nao e grade - o gizmo da Scene fica logo
    acima de "Persp" e e controle real.

    Fica o que esta fora do miolo (|dx| > 0,36 x passo da grade): o botao
    circular de expandir sub-assets fica na borda direita do bloco, e e um
    controle proprio.
    """
    if not icon_boxes or len(text_boxes) < 3:
        return list(icon_boxes)
    rotulos = sorted(text_boxes, key=lambda t: (round((t[1] + t[3]) / 2), t[0]))
    celulas = []                         # (cx do rotulo, topo do rotulo, passo)
    usados = set()
    for i, a in enumerate(rotulos):
        if i in usados:
            continue
        linha = [j for j, b in enumerate(rotulos)
                 if abs((b[1] + b[3]) / 2 - (a[1] + a[3]) / 2) <= 3 and abs((b[3] - b[1]) - (a[3] - a[1])) <= 3]
        for j in linha:
            usados.add(j)
        centros = sorted(((rotulos[j][0] + rotulos[j][2]) / 2, j) for j in linha)
        # Sequencia CONTINUA com passo de bloco (50-170 px) e tolerancia de 10%.
        # Medido na calibracao: um item da arvore cai na mesma linha dos blocos
        # e, com tolerancia de 20% e sem teto de passo, fechava uma falsa grade
        # de 3 com passo ~200 px, cuja celula engolia o botao "+" do Project.
        k = 0
        while k < len(centros) - 1:
            passo = centros[k + 1][0] - centros[k][0]
            if not 50 <= passo <= 170:
                k += 1
                continue
            fim = k + 1
            while fim + 1 < len(centros) and abs((centros[fim + 1][0] - centros[fim][0]) - passo) <= 0.1 * passo:
                fim += 1
            if fim - k + 1 >= 3:
                grupo = centros[k:fim + 1]
                for cx, j in grupo:
                    celulas.append((cx, rotulos[j][1], passo))
                # Sem celula extrapolada para bloco de rotulo nao lido: tentada,
                # no holdout L caiu sobre a seta de rolagem da grade (controle
                # real) e nao ajudou em nenhuma das outras 11 telas.
            k = fim
    if not celulas:
        return list(icon_boxes)
    saida = []
    for x, y, w, h, conf in icon_boxes:
        bx, by = x + w / 2, y + h / 2
        dentro = any(abs(bx - cx) <= 0.36 * passo and topo - 0.9 * passo <= by <= topo and y + h <= topo + 2
                     for cx, topo, passo in celulas)
        if dentro:
            if motivos is not None:
                motivos.append({"bbox": [x, y, w, h], "motivo": "fragmento_de_miniatura"})
            continue
        saida.append((x, y, w, h, conf))
    return saida


def _rotulado_ao_lado(box, text_boxes, folga=8):
    """Ícone colado a um rótulo na mesma linha: é parte do elemento rotulado.

    Icone de aba, de linha da Hierarchy, seta de dropdown com texto. Pela
    prioridade acordada (UIA/texto > SoM texto > icone), o texto ja representa
    o elemento e o icone sairia so como duplicata.
    """
    x1, y1, x2, y2 = box
    altura = max(1, y2 - y1)
    for tx1, ty1, tx2, ty2 in text_boxes:
        sobreposicao = min(y2, ty2) - max(y1, ty1)
        if sobreposicao < 0.5 * altura:
            continue
        vao = max(tx1 - x2, x1 - tx2)
        if vao <= folga:
            return True
    return False


def _overlaps_text(box, text_boxes, padding=5):
    x1, y1, x2, y2 = box
    area = max(1, (x2 - x1) * (y2 - y1))
    for tx1, ty1, tx2, ty2 in text_boxes:
        iw = max(0, min(x2, tx2 + padding) - max(x1, tx1 - padding))
        ih = max(0, min(y2, ty2 + padding) - max(y1, ty1 - padding))
        if iw * ih / area >= 0.12:
            return True
    return False

def _deduplicate_boxes(boxes, iou_threshold=0.35):
    kept = []
    for candidate in sorted(boxes, key=lambda item: (-item[4], item[1], item[0])):
        x, y, width, height = candidate[:4]
        candidate_xyxy = (x, y, x + width, y + height)
        overlaps_existing = False
        for existing in kept:
            ex, ey, ew, eh = existing[:4]
            if _box_iou(candidate_xyxy, (ex, ey, ex + ew, ey + eh)) >= iou_threshold:
                overlaps_existing = True
                break
        if overlaps_existing:
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: (item[1] // 20, item[0]))


# =====================================================================
# CORES DAS MARCAS SoM (IDEIAS/03 - item 1)
# =====================================================================
# O SoM entregava bbox e texto, e nenhuma cor. Sem isso o modelo sabe "roxo
# escuro" e nao sabe #1A0533 - e nao distingue botao de perigo, link e campo
# desabilitado, que sao a mesma caixa com cores diferentes.

SOM_COLORS = os.environ.get("SOM_COLORS", "1").strip().lower() not in ("0", "false", "no", "off")
SOM_COLOR_CLUSTERS = max(2, min(8, int(os.environ.get("SOM_COLOR_CLUSTERS", "3"))))
SOM_COLOR_MAX_PIXELS = max(256, int(os.environ.get("SOM_COLOR_MAX_PIXELS", "32768")))
# Contraste minimo para uma cor contar como "frente" e nao como variacao do
# proprio fundo. Entre as que passam, vence a MAIS FREQUENTE.
SOM_COLOR_MIN_CONTRAST = max(1.0, float(os.environ.get("SOM_COLOR_MIN_CONTRAST", "2.0")))

# Pillow moveu os enums de lugar entre versoes; FASTOCTREE=2, NEAREST=0.
_QUANTIZE_FASTOCTREE = getattr(getattr(Image, "Quantize", None), "FASTOCTREE", 2)
_RESAMPLE_NEAREST = getattr(getattr(Image, "Resampling", None), "NEAREST", 0)


def _relative_luminance(rgb):
    """Luminancia relativa da WCAG (a mesma usada em razao de contraste)."""
    channels = []
    for value in rgb[:3]:
        c = value / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(first, second):
    a = _relative_luminance(first)
    b = _relative_luminance(second)
    lighter, darker = (a, b) if a >= b else (b, a)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def region_palette(image, box, clusters=SOM_COLOR_CLUSTERS):
    """Cor de fundo, cor de frente e contraste de uma região da tela.

    Conta as cores EXATAS da regiao em vez de quantizar. Quantizador de octree
    devolve o centroide do cluster, nao um pixel que existe: para um botao
    #7B2FBE ele responde #7A2DBD. Como o ponto disto e entregar valor exato,
    isso seria repetir o erro do JPEG por outro caminho.

    O fundo e a cor mais frequente. A frente e a MAIS FREQUENTE entre as que
    contrastam com o fundo. Escolher "a de maior contraste" nao funciona: texto
    antialiasado espalha a cor em dezenas de tons e o extremo acaba sendo um
    pixel solto. Escolher por frequencia dentro das contrastantes acerta o tom
    que o olho realmente ve como sendo o do texto.

    Precisao, medida: o FUNDO sai exato - e o caso que importa, porque botao,
    painel e campo sao regioes chapadas, e e a cor de fundo que separa botao de
    perigo (vermelho) de link (azul) e de campo desabilitado (cinza). A FRENTE
    de texto antialiasado e aproximada por natureza: numa fonte de 11px a haste
    e fina demais para sobrar pixel na cor pura, entao #F3E8FF volta como
    #E6DBF3. Perto, nao igual. Nao confie no fg para reproduzir cor de projeto.

    "bg_share" diz que fracao da regiao o fundo ocupa: perto de 1.0 e superficie
    chapada e a resposta vale; baixo e regiao heterogenea (foto, gradiente,
    video) onde "cor dominante" nao quer dizer grande coisa.

    Regiao com cores demais para contar cai para clusters aproximados e marca
    "exact": false.
    """
    left, top, right, bottom = (int(value) for value in box)
    left = max(0, left)
    top = max(0, top)
    right = min(image.size[0], right)
    bottom = min(image.size[1], bottom)
    if right - left < 2 or bottom - top < 2:
        return None

    try:
        crop = image.crop((left, top, right, bottom)).convert("RGB")
        total = crop.size[0] * crop.size[1]
        if total > SOM_COLOR_MAX_PIXELS:
            # NEAREST de proposito: qualquer reamostragem suave inventaria cor.
            factor = (SOM_COLOR_MAX_PIXELS / total) ** 0.5
            crop = crop.resize(
                (max(1, int(crop.size[0] * factor)), max(1, int(crop.size[1] * factor))),
                _RESAMPLE_NEAREST)
            total = crop.size[0] * crop.size[1]
        counted = crop.getcolors(maxcolors=total)
    except Exception:
        return None

    if counted:
        counted.sort(reverse=True)
        background = tuple(counted[0][1])
        foreground = background
        for count, rgb in counted[1:]:
            # counted esta ordenado por frequencia: a primeira que contrasta ja
            # e a mais frequente entre as contrastantes.
            if count > 1 and _contrast_ratio(background, rgb) >= SOM_COLOR_MIN_CONTRAST:
                foreground = tuple(rgb)
                break
        return {
            "bg": "#%02X%02X%02X" % background,
            "fg": "#%02X%02X%02X" % foreground,
            "contrast": _contrast_ratio(background, foreground),
            "bg_share": round(counted[0][0] / max(1, total), 3),
            "exact": True,
        }

    # Cores demais para contar: nao existe cor dominante real nesta regiao.
    try:
        quantized = crop.quantize(colors=clusters, method=_QUANTIZE_FASTOCTREE)
        palette = quantized.getpalette() or []
        groups = sorted(quantized.getcolors() or [], reverse=True)
    except Exception:
        return None
    entries = []
    for _count, index in groups:
        base = index * 3
        if base + 2 < len(palette):
            entries.append(tuple(palette[base:base + 3]))
    if not entries:
        return None
    background = entries[0]
    foreground = background
    best_ratio = -1.0
    for rgb in entries[1:]:
        ratio = _contrast_ratio(background, rgb)
        if ratio > best_ratio:
            best_ratio, foreground = ratio, rgb
    return {
        "bg": "#%02X%02X%02X" % background,
        "fg": "#%02X%02X%02X" % foreground,
        "contrast": _contrast_ratio(background, foreground),
        "exact": False,
    }



# =====================================================================
# GRAFO DE CENA POR GEOMETRIA (IDEIAS/03 - item 2)
# =====================================================================
# As marcas do SoM sao todas FOLHAS: texto e icone de 9 a 64 px. Nenhum painel
# e detectado, entao uma arvore por CONTENCAO pura nao produziria quase nada -
# quase nenhuma marca esta dentro de outra. O que existe de verdade numa tela e
# ALINHAMENTO: rotulo e campo na mesma linha, linhas empilhadas num bloco. E
# isso que se agrupa aqui, sem modelo, sem API de sistema e sem efeito colateral.

# DESLIGADO por padrao. A avaliacao com modelo no laco deu 100% de acerto nos
# DOIS bracos (24 perguntas, cenas sinteticas) - teto, ou seja, o teste nao
# conseguiu medir diferenca, o que NAO e o mesmo que provar que nao ha
# diferenca. O que esta medido com certeza e o custo: ~+90% de token. Pagar
# custo certo por beneficio desconhecido e ruim, entao o padrao e nao pagar.
# Ligue por requisicao (?groups=1) quando a tela for tabela ou grade densa.
SOM_GROUPS = os.environ.get("SOM_GROUPS", "0").strip().lower() not in ("0", "false", "no", "off")
# Minimo de linhas para valer a pena EMITIR a lista de grupos. Medido: numa
# tela sem nenhuma linha (arvore de projeto, itens soltos) os grupos ainda
# custavam +42% de token entregando ZERO linha legivel. Calcular e barato
# (8 ms); o que custa e mandar. Com 1, so o caso inutil e suprimido - suprimir
# mais que isso depende da avaliacao com modelo no laco (avaliar_grafo.py).
SOM_GROUPS_MIN_ROWS = max(0, int(os.environ.get("SOM_GROUPS_MIN_ROWS", "1")))
# Fracao de sobreposicao vertical para duas marcas contarem como mesma linha.
# 0.6 saiu de varredura medida, nao de escolha: em 300 cenas com ruido
# combinado a precisao sobe de 0.944 para 0.969 contra recall de 0.947 para
# 0.936. Associacao ERRADA engana o modelo; associacao perdida so falta.
SOM_ROW_OVERLAP = max(0.1, min(1.0, float(os.environ.get("SOM_ROW_OVERLAP", "0.6"))))
# Teto absoluto de vao horizontal, em alturas medianas: acima disso nunca junta,
# por mais alinhadas que as marcas estejam.
SOM_ROW_GAP_MAX = max(1.0, float(os.environ.get("SOM_ROW_GAP_MAX", "12.0")))
# Quanto um vao precisa ser maior que a MEDIANA dos vaos da propria linha para
# contar como quebra de coluna. Largura de gutter nao e sinal confiavel - varia
# de formulario para formulario -, mas "este vao destoa dos outros desta linha"
# e. Um piso em alturas medianas evita quebrar em ruido de sub-pixel.
SOM_ROW_SPLIT_RATIO = max(1.1, float(os.environ.get("SOM_ROW_SPLIT_RATIO", "2.5")))
SOM_ROW_SPLIT_FLOOR = max(0.1, float(os.environ.get("SOM_ROW_SPLIT_FLOOR", "1.0")))
SOM_BLOCK_OVERLAP = max(0.1, min(1.0, float(os.environ.get("SOM_BLOCK_OVERLAP", "0.35"))))
SOM_BLOCK_GAP = max(0.2, float(os.environ.get("SOM_BLOCK_GAP", "1.2")))


class _Union:
    """Union-find: 'a perto de b' e 'b perto de c' tem que cair no mesmo grupo."""

    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first, second):
        root_a, root_b = self.find(first), self.find(second)
        if root_a != root_b:
            self.parent[root_b] = root_a

    def clusters(self):
        buckets = {}
        for index in range(len(self.parent)):
            buckets.setdefault(self.find(index), []).append(index)
        return list(buckets.values())


def _span_overlap(a_start, a_end, b_start, b_end):
    """Sobreposição de dois intervalos como fração do MENOR deles."""
    inter = min(a_end, b_end) - max(a_start, b_start)
    if inter <= 0:
        return 0.0
    smaller = min(a_end - a_start, b_end - b_start)
    return inter / smaller if smaller > 0 else 0.0


def _span_gap(a_start, a_end, b_start, b_end):
    """Vão entre dois intervalos; zero se eles se tocam ou sobrepõem."""
    return max(0.0, max(a_start, b_start) - min(a_end, b_end))


def _cluster_box(boxes, indexes):
    x0 = min(boxes[i][0] for i in indexes)
    y0 = min(boxes[i][1] for i in indexes)
    x1 = max(boxes[i][2] for i in indexes)
    y1 = max(boxes[i][3] for i in indexes)
    return [int(x0), int(y0), int(x1 - x0), int(y1 - y0)]


def group_marks(marks, enabled=None):
    """Anota cada marca com sua linha e seu bloco. Devolve a contagem.

    `enabled` sobrepoe SOM_GROUPS para uma chamada so, que e como ?groups=1
    liga o agrupamento por requisicao sem mexer no padrao do servidor.

    Duas passagens de union-find: marcas viram linhas (sobreposicao vertical +
    proximidade horizontal), linhas viram blocos (sobreposicao horizontal +
    proximidade vertical). Os limiares sao relativos a altura mediana das
    marcas, entao DPI e tamanho de fonte se ajustam sozinhos.
    """
    for mark in marks:
        mark.pop("group", None)
        mark.pop("block", None)
    ligado = SOM_GROUPS if enabled is None else bool(enabled)
    if not ligado or len(marks) < 2:
        return {"rows": 0, "blocks": 0}

    boxes = []
    for mark in marks:
        x, y, w, h = mark["bbox"]
        boxes.append((x, y, x + max(1, w), y + max(1, h)))

    heights = sorted(box[3] - box[1] for box in boxes)
    unit = max(8.0, float(heights[len(heights) // 2]))

    # --- passagem 1a: marcas -> faixas horizontais (so sobreposicao vertical) ---
    band_union = _Union(len(boxes))
    for i in range(len(boxes)):
        _ax0, ay0, _ax1, ay1 = boxes[i]
        for j in range(i + 1, len(boxes)):
            _bx0, by0, _bx1, by1 = boxes[j]
            if _span_overlap(ay0, ay1, by0, by1) >= SOM_ROW_OVERLAP:
                band_union.union(i, j)

    # --- passagem 1b: cada faixa vira uma ou mais linhas ---
    row_clusters = []
    gap_max = SOM_ROW_GAP_MAX * unit
    gap_floor = SOM_ROW_SPLIT_FLOOR * unit
    for band in band_union.clusters():
        band = sorted(band, key=lambda index: boxes[index][0])
        if len(band) == 1:
            row_clusters.append(band)
            continue
        gaps = [_span_gap(boxes[band[k]][0], boxes[band[k]][2],
                          boxes[band[k + 1]][0], boxes[band[k + 1]][2])
                for k in range(len(band) - 1)]
        positive = sorted(value for value in gaps if value > 0)
        median_gap = positive[len(positive) // 2] if positive else 0.0
        limit = max(gap_floor, SOM_ROW_SPLIT_RATIO * median_gap)
        current = [band[0]]
        for k, gap in enumerate(gaps):
            # Quebra por outlier relativo, ou por distancia absoluta demais.
            if gap > gap_max or gap > limit:
                row_clusters.append(current)
                current = []
            current.append(band[k + 1])
        row_clusters.append(current)
    row_boxes = [_cluster_box(boxes, cluster) for cluster in row_clusters]
    row_spans = [(box[0], box[1], box[0] + box[2], box[1] + box[3]) for box in row_boxes]

    # --- passagem 2: linhas -> blocos ---
    block_union = _Union(len(row_clusters))
    block_gap = SOM_BLOCK_GAP * unit
    for i in range(len(row_spans)):
        ax0, ay0, ax1, ay1 = row_spans[i]
        for j in range(i + 1, len(row_spans)):
            bx0, by0, bx1, by1 = row_spans[j]
            if _span_overlap(ax0, ax1, bx0, bx1) < SOM_BLOCK_OVERLAP:
                continue
            if _span_gap(ay0, ay1, by0, by1) > block_gap:
                continue
            block_union.union(i, j)

    # Linha de uma marca so nao vira grupo: nao acrescenta nada e custa token.
    named_rows = 0
    for row_index, cluster in enumerate(row_clusters):
        block_id = f"b{block_union.find(row_index) + 1}"
        row_id = f"r{row_index + 1}" if len(cluster) > 1 else None
        if row_id:
            named_rows += 1
        for mark_index in cluster:
            if row_id:
                marks[mark_index]["group"] = row_id
            marks[mark_index]["block"] = block_id

    if named_rows < SOM_GROUPS_MIN_ROWS:
        # Agrupamento que nao produziu linha legivel nao vale o token. A
        # supressao tem que ser AQUI e nao na hora de montar a lista: se as
        # marcas mantivessem "block" apontando para um grupo nao enviado,
        # ficaria referencia morta no payload.
        for mark in marks:
            mark.pop("group", None)
            mark.pop("block", None)
        return {"rows": 0, "blocks": 0, "suprimido": True}

    blocks = {mark.get("block") for mark in marks if mark.get("block")}
    return {"rows": named_rows, "blocks": len(blocks)}


def collect_groups(marks):
    """Reconstrói a lista de grupos a partir das anotações já nas marcas.

    Separado de group_marks para que quem so quer a lista nao repita a parte
    O(n²). Aqui e uma varredura linear.

    """
    rows, blocks = {}, {}
    order = {}
    for position, mark in enumerate(marks):
        order[mark.get("tag")] = position
        row_id = mark.get("group")
        if row_id:
            rows.setdefault(row_id, []).append(mark)
        block_id = mark.get("block")
        if block_id:
            blocks.setdefault(block_id, []).append(mark)

    def box_of(members):
        x0 = min(m["bbox"][0] for m in members)
        y0 = min(m["bbox"][1] for m in members)
        x1 = max(m["bbox"][0] + m["bbox"][2] for m in members)
        y1 = max(m["bbox"][1] + m["bbox"][3] for m in members)
        return [x0, y0, x1 - x0, y1 - y0]

    groups = []
    for row_id, members in rows.items():
        # Ordem de leitura: da esquerda para a direita dentro da linha.
        members = sorted(members, key=lambda m: m["bbox"][0])
        groups.append({
            "id": row_id,
            "kind": "row",
            "box": box_of(members),
            "tags": [m["tag"] for m in members],
            "text": "  ".join(str(m.get("text") or "").strip()
                              for m in members if str(m.get("text") or "").strip()),
        })

    for block_id, members in blocks.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda m: (m["bbox"][1], m["bbox"][0]))
        inner_rows = []
        for m in members:
            row_id = m.get("group")
            if row_id and row_id not in inner_rows:
                inner_rows.append(row_id)
        # Sem repetir as tags: elas ja estao nas linhas listadas e no campo
        # "block" de cada marca. Repetir aqui custava token e nao informava nada.
        groups.append({
            "id": block_id,
            "kind": "block",
            "box": box_of(members),
            "rows": inner_rows,
        })

    groups.sort(key=lambda g: (g["box"][1], g["box"][0]))
    return groups



# =====================================================================
# HOVER PING - PERCEPCAO ATIVA (IDEIAS/03 - item 3)
# =====================================================================
# Nenhuma analise estatica de screenshot responde "o que aqui e clicavel": um
# retangulo colorido e um botao sao os mesmos pixels. Mas o botao REAGE ao
# hover. Em vez de olhar com mais atencao, perturba-se e observa-se a resposta.
#
# DESLIGADO por padrao, e de proposito: diferente de todo o resto do /som, isto
# MEXE no mouse e tem efeito colateral de verdade.

SOM_HOVER_PROBE = os.environ.get("SOM_HOVER_PROBE", "0").strip().lower() not in ("0", "false", "no", "off")
# So candidato de confianca INTERMEDIARIA. Abaixo do piso o Canny quase
# certamente errou; no teto acima ja passou. Sondar tela inteira seria caro,
# lento e cheio de efeito colateral - por isso nunca ha varredura global.
HOVER_CONF_MIN = max(0.0, min(1.0, float(os.environ.get("SOM_HOVER_CONF_MIN", "0.65"))))
# Teto em 1.0, nao em 0.85. Com SOM_ICON_MIN_CONFIDENCE=0.85 e a confianca
# quantizada em {0, .25, .5, .75, 1.0}, so icone de 1.0 entrava no SoM, e a
# faixa [0.65, 0.85) ficava com intersecao VAZIA - a sondagem era no-op. Quem
# tem duvida util e o candidato abaixo do limiar, resgatado por
# include_candidates; quem tirou 4 de 4 nao precisa ser sondado.
HOVER_CONF_MAX = max(0.0, min(1.0, float(os.environ.get("SOM_HOVER_CONF_MAX", "1.0"))))
# Botao moderno nao muda na hora: transicao de 150 a 300 ms. Sem esta espera a
# segunda captura pega o estado antigo e todo elemento parece inerte.
# 180 ms medido em Windows real: 50 ms perde o inicio da animacao (delta zero),
# 100 ms fica no limiar, 150-220 ms e o pico. 180 pega a transicao inteira e
# poupa ~25% do tempo contra 220.
HOVER_SETTLE_MS = max(0, int(os.environ.get("SOM_HOVER_SETTLE_MS", "180")))
# 6 sondagens a ~180 ms mantem o lote abaixo de ~1.1 s.
HOVER_MAX_PROBES = max(1, int(os.environ.get("SOM_HOVER_MAX_PROBES", "6")))
HOVER_BUDGET_MS = max(200, int(os.environ.get("SOM_HOVER_BUDGET_MS", "4000")))
# Reacao = mudou a FORMA (bits de dHash) OU o PREENCHIMENTO (canal de cor).
# 2 bits: elemento estatico mediu 0 bits em Windows real (0% de falso
# positivo), e botao chapado move 0 a 1 - quem separa esses e o canal de cor.
HOVER_MIN_DELTA = max(1, int(os.environ.get("SOM_HOVER_MIN_DELTA", "2")))
HOVER_MIN_COLOR = max(1.0, float(os.environ.get("SOM_HOVER_MIN_COLOR", "6.0")))
HOVER_PAD = max(2, int(os.environ.get("SOM_HOVER_PAD", "6")))
# Soltar o confinamento por retangulo ao detectar cursor preso. Ligado por
# padrao porque sem isso a restauracao do ponteiro falha em silencio.
HOVER_UNCLIP = os.environ.get("SOM_HOVER_UNCLIP", "1").strip().lower() not in ("0", "false", "no", "off")

# Ligado enquanto uma skill esta sendo replayada: sondar no meio de um replay
# mexeria o mouse entre a verificacao e o clique.
_replay_em_curso = threading.local()


def _em_replay():
    return bool(getattr(_replay_em_curso, "ativo", False))


def _hover_fingerprint(monitor, box):
    """Assinatura do recorte em volta do candidato: forma E cor.

    Duas medidas, porque uma so nao basta:

    - dHash pega mudanca de FORMA - contorno de foco que aparece, seta que
      surge, sublinhado de link.
    - media de cor pega mudanca de PREENCHIMENTO - e este e o caso comum:
      botao que clareia no hover mantem exatamente a mesma borda, entao o
      dHash dele nao move UM BIT. A mesma cegueira do item 1 ("dHash enxerga
      borda, nao cor") reaparece aqui, e aqui ela seria fatal: o detector
      declararia inerte justamente o botao que mais reage.

    O recorte e local de proposito: o realce do hover e pequeno e sumiria num
    hash de tela inteira.
    """
    image, _window, origin = _signature_source_image(monitor=monitor)
    x, y, w, h = box
    left = x - origin[0] - HOVER_PAD
    top = y - origin[1] - HOVER_PAD
    right = left + w + HOVER_PAD * 2
    bottom = top + h + HOVER_PAD * 2
    left = max(0, min(left, image.size[0] - 2))
    top = max(0, min(top, image.size[1] - 2))
    right = max(left + 2, min(right, image.size[0]))
    bottom = max(top + 2, min(bottom, image.size[1]))
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    media = ImageStat.Stat(crop).mean[:3]
    return _dhash_bits(crop), tuple(media)


def _hover_delta(antes, depois):
    """Distancia de forma (bits) e de cor (por canal) entre duas assinaturas."""
    bits = _hamming(antes[0], depois[0])
    cor = max(abs(a - b) for a, b in zip(antes[1], depois[1]))
    return bits, round(cor, 1)


def hover_probe(marks, monitor="1", conf_min=None, conf_max=None,
                max_probes=None, settle_ms=None, budget_ms=None):
    """Descobre quais candidatos duvidosos reagem ao ponteiro.

    Anota `interactive` nas marcas sondadas. Nao varre a tela: so entram
    candidatos na faixa de confianca intermediaria, ate um teto de sondagens e
    um orcamento de tempo.
    """
    if _em_replay():
        return {"ok": False, "error_code": "replay_em_curso",
                "error": "sondagem nao roda durante replay de skill: mexer o "
                         "mouse entre a verificacao e o clique quebraria o passo"}

    baixo = HOVER_CONF_MIN if conf_min is None else float(conf_min)
    alto = HOVER_CONF_MAX if conf_max is None else float(conf_max)
    teto = HOVER_MAX_PROBES if max_probes is None else max(1, int(max_probes))
    espera = (HOVER_SETTLE_MS if settle_ms is None else max(0, int(settle_ms))) / 1000.0
    orcamento = (HOVER_BUDGET_MS if budget_ms is None else max(200, int(budget_ms))) / 1000.0

    candidatos = [m for m in marks
                  if m.get("type") in ("icon", "icon_candidate")
                  and baixo <= float(m.get("confidence") or 0.0) < alto]
    candidatos.sort(key=lambda m: -float(m.get("confidence") or 0.0))
    candidatos = candidatos[:teto]
    if not candidatos:
        return {"ok": True, "action": "hover_probe", "probed": 0, "results": [],
                "reason": "nenhum candidato na faixa de confianca"}

    origem = POINT()
    user32.GetCursorPos(byref(origem))
    inicio = time.time()
    resultados = []
    abortou = None
    saida_clip = None

    try:
        for mark in candidatos:
            if time.time() - inicio > orcamento:
                abortou = "orcamento de tempo esgotado"
                break
            cx, cy = mark["center"]
            antes = _hover_fingerprint(monitor, mark["bbox"])
            with input_action_lock:
                win32_mouse_move(cx, cy)
            time.sleep(espera)

            # O ponteiro chegou onde foi mandado? Janela DirectX/Unity com
            # SetCapture ou ClipCursor prende o cursor; sondar preso mediria
            # sempre o mesmo lugar e devolveria lixo.
            atual = POINT()
            user32.GetCursorPos(byref(atual))
            if abs(atual.x - cx) > 4 or abs(atual.y - cy) > 4:
                abortou = "cursor preso pela janela (SetCapture/ClipCursor)"
                # ClipCursor(NULL) solta o confinamento por RETANGULO, e e o
                # que permite devolver o ponteiro no finally. NAO resolve
                # SetCapture, que e por thread e so o dono libera. Fica atras
                # de uma flag porque desconfinar por conta propria atrapalha
                # jogo que confina o cursor de proposito.
                if HOVER_UNCLIP:
                    try:
                        user32.ClipCursor(None)
                        saida_clip = True
                    except Exception:
                        saida_clip = False
                break

            depois = _hover_fingerprint(monitor, mark["bbox"])
            bits, cor = _hover_delta(antes, depois)
            interativo = bits >= HOVER_MIN_DELTA or cor >= HOVER_MIN_COLOR
            mark["interactive"] = interativo
            mark["hover_delta"] = {"forma": bits, "cor": cor}
            resultados.append({"tag": mark.get("tag"), "forma": bits, "cor": cor,
                               "interactive": interativo,
                               "confidence": mark.get("confidence")})
    finally:
        # Devolver o ponteiro e obrigatorio: a sondagem nao pode deixar rastro.
        with input_action_lock:
            win32_mouse_move(origem.x, origem.y)

    saida = {"ok": True, "action": "hover_probe", "probed": len(resultados),
             "interactive": sum(1 for r in resultados if r["interactive"]),
             "results": resultados, "elapsed_ms": int((time.time() - inicio) * 1000),
             "cursor_restored": [origem.x, origem.y]}
    if abortou:
        saida["aborted"] = abortou
    if saida_clip is not None:
        saida["clip_liberado"] = saida_clip
    return saida


def inspect_screen_som(monitor="1", draw_badges=True, include_icons=True, groups=None,
                       include_candidates=False):
    """Extrai textos (WinOCR) e botões/ícones gráficos (OpenCV Contours) gerando Set-of-Mark visual completo."""
    ensure_desktop_access()
    # Antes isto codificava a tela em JPEG 95 e decodificava de volta so para ter
    # um PIL.Image. A volta custa alguns ms por chamada e, pior, ADULTERA A COR:
    # extrair #1A0533 de um JPEG devolve #1A0633. Como o ponto das cores abaixo e
    # justamente entregar valor exato, a captura tem que ser sem perda.
    img = capture_raw_pil_image(monitor=monitor, draw_cursor=False).convert("RGB")
    mw, mh = img.size
    
    marks = _ListaComCota()
    tag_id = 1
    
    draw_img = img.copy() if draw_badges else None
    if draw_badges:
        draw = ImageDraw.Draw(draw_img, 'RGBA')
        try:
            font = ImageFont.truetype("arial.ttf", 11)
        except Exception:
            font = ImageFont.load_default()
            
    ox, oy, _, _ = get_monitor_geom(monitor)
    occupied_boxes = []
    icon_candidates = []

    if winocr:
        ocr_lang = os.environ.get("SOM_OCR_LANG", "pt-BR")
        try:
            original_candidates = _ocr_candidates(img, lang=ocr_lang)
        except Exception:
            original_candidates = _ocr_candidates(img, lang="en")

        char_count = sum(len(item["text"]) for item in original_candidates)
        boost_mode = os.environ.get("SOM_OCR_BOOST", "auto").strip().lower()
        use_boost = boost_mode in ("always", "light") or (
            boost_mode == "auto" and (len(original_candidates) < 6 or char_count < 40)
        )
        all_candidates = list(original_candidates)
        if use_boost:
            for variant, variant_scale, source in _enhanced_ocr_images(img, aggressive=False):
                try:
                    all_candidates.extend(_ocr_candidates(
                        variant, lang=ocr_lang, scale=variant_scale, source=source))
                except Exception:
                    pass
            enhanced_chars = sum(len(item["text"]) for item in _merge_ocr_candidates(all_candidates))
            if enhanced_chars < 25 and boost_mode != "light":
                for variant, variant_scale, source in _enhanced_ocr_images(img, aggressive=True)[1:]:
                    try:
                        all_candidates.extend(_ocr_candidates(
                            variant, lang=ocr_lang, scale=variant_scale, source=source))
                    except Exception:
                        pass

        for candidate in _merge_ocr_candidates(all_candidates):
            text = candidate["text"]
            min_x, min_y, max_x, max_y = candidate["box"]
            bw = max_x - min_x
            bh = max_y - min_y
            abs_cx = ox + int(min_x + bw / 2)
            abs_cy = oy + int(min_y + bh / 2)
            if len(marks) >= SOM_MAX_MARKS:
                marks.truncation = _aviso_truncamento(
                    "limite_de_marcas", len(marks), SOM_MAX_MARKS,
                    {"stage": "texto"})
                break
            occupied_boxes.append((min_x, min_y, max_x, max_y))
            text_mark = {
                "tag": tag_id,
                "type": "text",
                "text": text,
                "source": candidate["source"],
                "bbox": [int(ox + min_x), int(oy + min_y), int(bw), int(bh)],
                "center": [abs_cx, abs_cy]
            }
            if SOM_COLORS:
                # As coordenadas do OCR sao locais ao monitor, iguais as da
                # imagem: o recorte nao passa pelo offset ox/oy.
                palette = region_palette(img, (min_x, min_y, max_x, max_y))
                if palette:
                    text_mark.update(palette)
            marks.append(text_mark)
            if draw_badges:
                color = (255, 60, 60, 220) if tag_id % 3 == 0 else ((60, 220, 100, 220) if tag_id % 3 == 1 else (0, 180, 255, 220))
                draw.rectangle([min_x, min_y, max_x, max_y], outline=color[:3], width=1)
                badge_w = 16 if tag_id < 10 else (22 if tag_id < 100 else 28)
                badge_h = 13
                bx = max(0, min_x - badge_w - 2)
                by = min_y
                draw.rectangle([bx, by, bx + badge_w, by + badge_h], fill=(0, 0, 0, 220), outline=color[:3])
                draw.text((bx + 2, by), str(tag_id), fill=(255, 255, 255), font=font)
            tag_id += 1

    # Detecção de Ícones Gráficos sem texto via OpenCV Contours
    if include_icons and cv2 and np:
        try:
            np_img = np.array(img)
            gray = cv2.cvtColor(np_img, cv2.COLOR_RGB2GRAY)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            median = float(np.median(blurred))
            lower = int(max(20, 0.66 * median))
            upper = int(min(220, max(lower + 30, 1.33 * median)))
            edges = cv2.Canny(blurred, lower, upper)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            dilated = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
            v2 = detector_de_icone_para((ox, oy, mw, mh)) == "v2"
            marks.icon_detector = "v2" if v2 else "v1"
            if v2:
                dilated = _remover_linhas_longas(dilated, SOM_ICON_V2_LINE_PX)
            component_count, _, component_stats, _ = cv2.connectedComponentsWithStats(
                dilated, connectivity=8)

            icon_boxes = []
            descartados = [] if (v2 and SOM_ICON_DEBUG) else None
            menus_locais = [(l - ox, t - oy, r - ox, b - oy)
                            for l, t, r, b in retangulos_de_menus_nativos()] if v2 else []
            dpi_scale = max(0.75, min(2.5, min(mw / 1920.0, mh / 1080.0)))
            min_size = max(8, round(9 * dpi_scale))
            max_size = max(42, round(64 * dpi_scale))
            min_confidence = SOM_ICON_V2_MIN_CONFIDENCE if v2 else max(0.5, min(1.0, float(
                os.environ.get("SOM_ICON_MIN_CONFIDENCE", "0.85"))))
            for component_id in range(1, component_count):
                x, y, w, h, component_pixels = (
                    int(value) for value in component_stats[component_id])
                caixa = (x, y, x + w, y + h)
                if menus_locais and any(ml <= x + w / 2 <= mr and mt <= y + h / 2 <= mb
                                        for ml, mt, mr, mb in menus_locais):
                    if descartados is not None:
                        descartados.append({"bbox": [x, y, w, h], "motivo": "menu_nativo"})
                    continue
                # v2: o "⋮" sao tres pontos empilhados, estreitos demais para
                # o filtro de tamanho. Aceito so se a FORMA confirmar.
                if v2 and w <= 6 and h <= round(16 * dpi_scale) + 2 and h >= 6:
                    if not _parece_tres_pontos(gray, x, y, w, h, dpi_scale):
                        if descartados is not None:
                            descartados.append({"bbox": [x, y, w, h], "motivo": "nao_e_tres_pontos"})
                        continue
                    if _overlaps_text(caixa, occupied_boxes) or _rotulado_ao_lado(caixa, occupied_boxes):
                        if descartados is not None:
                            descartados.append({"bbox": [x, y, w, h], "motivo": "texto"})
                        continue
                    icon_boxes.append((x, y, w, h, 1.0))
                    continue
                # Filtro de tamanho de ícone de UI (entre 10 e 36 px de largura e altura)
                if min_size <= w <= max_size and min_size <= h <= max_size and 0.35 <= (w / h) <= 2.85:
                    # Verifica se não colide com texto já detectado por OCR
                    cx = x + w / 2
                    cy = y + h / 2
                    in_text = _overlaps_text(caixa, occupied_boxes) or (
                        v2 and _rotulado_ao_lado(caixa, occupied_boxes))
                    if in_text and descartados is not None:
                        descartados.append({"bbox": [x, y, w, h], "motivo": "texto"})
                    density = component_pixels / max(1, w * h)
                    patch_contrast = float(gray[y:y + h, x:x + w].std())
                    score = 0
                    score += 1 if 0.08 <= density <= 0.72 else 0
                    score += 1 if 0.5 <= (w / h) <= 2.0 else 0
                    score += 1 if patch_contrast >= 14 else 0
                    score += 1 if w >= round(12 * dpi_scale) and h >= round(12 * dpi_scale) else 0
                    confidence = round(score / 4.0, 2)
                    if not in_text and confidence >= min_confidence:
                        icon_boxes.append((x, y, w, h, confidence))
                    elif not in_text and descartados is not None:
                        descartados.append({"bbox": [x, y, w, h], "motivo": "score",
                                            "confidence": confidence})
                    if not in_text and confidence < min_confidence and include_candidates and \
                            HOVER_CONF_MIN <= confidence < min_confidence:
                        # Reprovado no heuristico, mas nao por pouco a ponto de
                        # ser ruido. E exatamente aqui que mora a duvida que a
                        # sondagem por hover existe para resolver.
                        icon_candidates.append((x, y, w, h, confidence))
                        
            # Ordena ícones e adiciona ao conjunto SoM com destaque dourado
            icon_boxes = _deduplicate_boxes(icon_boxes)
            if v2:
                icon_boxes = _filtro_contextual_v2(icon_boxes, occupied_boxes, dilated,
                                                   img, descartados)
            if descartados is not None:
                marks.icon_debug = descartados
            if icon_candidates:
                # Entram so quando pedidos (caminho da sondagem), nunca no
                # /state normal: candidato reprovado nao deve custar token de
                # quem nao vai sonda-lo.
                for cx_, cy_, cw_, ch_, conf_ in _deduplicate_boxes(icon_candidates):
                    icon_boxes.append((cx_, cy_, cw_, ch_, conf_))
            for ix, iy, iw, ih, confidence in icon_boxes:
                if len(marks) >= SOM_MAX_MARKS:
                    marks.truncation = _aviso_truncamento(
                        "limite_de_marcas", len(marks), SOM_MAX_MARKS,
                        {"stage": "icones"})
                    break
                abs_cx = ox + int(ix + iw / 2)
                abs_cy = oy + int(iy + ih / 2)
                duvidoso = confidence < min_confidence
                mark_item = {
                    "tag": tag_id,
                    "type": "icon_candidate" if duvidoso else "icon",
                    "text": f"[Icon {tag_id}]",
                    "confidence": confidence,
                    "bbox": [int(ox + ix), int(oy + iy), int(iw), int(ih)],
                    "center": [abs_cx, abs_cy]
                }
                if SOM_COLORS:
                    palette = region_palette(img, (ix, iy, ix + iw, iy + ih))
                    if palette:
                        mark_item.update(palette)
                marks.append(mark_item)
                
                if draw_badges:
                    icon_color = (255, 190, 0) # Dourado para ícones visuais
                    draw.rectangle([ix, iy, ix + iw, iy + ih], outline=icon_color, width=1)
                    badge_w = 16 if tag_id < 10 else (22 if tag_id < 100 else 28)
                    badge_h = 13
                    bx = max(0, ix - badge_w - 2)
                    by = iy
                    draw.rectangle([bx, by, bx + badge_w, by + badge_h], fill=(0, 0, 0, 220), outline=icon_color)
                    draw.text((bx + 2, by), str(tag_id), fill=(255, 255, 255), font=font)
                    
                tag_id += 1
        except Exception:
            pass

    # Marcas de janelas excluidas (o terminal do proprio agente) saem ANTES das
    # tags: nao consomem numero, nao viram alvo de click_text/click_tag.
    if exclusao_ativa() and marks:
        juiz = _JuizDeExclusao()
        antes = len(marks)
        marks[:] = [m for m in marks
                    if not juiz.ponto_excluido(m["center"][0], m["center"][1])]
        marks.excluded_marks = antes - len(marks)

    # A ordem do OCR/contornos varia. As tags persistentes são atribuídas antes
    # de gerar a imagem, então inserir/remover outro elemento não renumera tudo.
    assign_stable_som_tags(marks, monitor=monitor)
    group_marks(marks, enabled=groups)
    for mark in marks:
        if mark.get("type") == "icon":
            mark["text"] = f"[Icon {mark['tag']}]"

    if draw_badges:
        draw_img = img.copy()
        draw = ImageDraw.Draw(draw_img, 'RGBA')
        try:
            font = ImageFont.truetype("arial.ttf", 11)
        except Exception:
            font = ImageFont.load_default()
        for mark in marks:
            tag = mark["tag"]
            x, y, width, height = mark["bbox"]
            x -= ox
            y -= oy
            if mark.get("type") == "icon":
                color = (255, 190, 0)
            elif tag % 3 == 0:
                color = (255, 60, 60)
            elif tag % 3 == 1:
                color = (60, 220, 100)
            else:
                color = (0, 180, 255)
            draw.rectangle([x, y, x + width, y + height], outline=color, width=1)
            badge_width = 16 if tag < 10 else (22 if tag < 100 else 28)
            badge_x = max(0, x - badge_width - 2)
            draw.rectangle([badge_x, y, badge_x + badge_width, y + 13],
                           fill=(0, 0, 0, 220), outline=color)
            draw.text((badge_x + 2, y), str(tag), fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    if draw_badges:
        draw_img.save(buf, format="JPEG", quality=85)
    else:
        img.save(buf, format="JPEG", quality=85)

    return marks, buf.getvalue()


# Minimo de controles UIA DENTRO da area cliente para o modo auto confiar na UIA.
UIA_MIN_ELEMENTOS_CLIENTE = 4
# Janela que ja se mostrou "so moldura" nao e varrida de novo por este tempo: no
# Unity a varredura custava ~900 ms por /state para achar sempre a mesma coisa.
UIA_SO_MOLDURA_TTL = max(0.0, float(os.environ.get("UIA_SO_MOLDURA_TTL", "30")))
_uia_so_moldura = {}
_uia_so_moldura_lock = threading.Lock()


def _chave_da_janela(janela):
    """Hwnd + retângulo: se a janela mover ou redimensionar, a chave muda e a
    moldura é varrida de novo - as coordenadas guardadas nunca ficam velhas."""
    if not janela or not janela.get("hwnd"):
        return None
    return (janela.get("hwnd"), janela.get("left"), janela.get("top"),
            janela.get("width"), janela.get("height"))


def elementos_na_area_cliente(elementos, hwnd):
    """Quantos elementos UIA têm o centro dentro da área cliente da janela.

    Existe por causa do Unity, medido: a UIA dele expoe 15 elementos, e TODOS
    sao moldura do Windows - menu do sistema, minimizar/restaurar/fechar e a
    barra File...Help, que no Win32 fica fora da area cliente. Contando o
    total, o modo auto escolhia a UIA e o agente recebia 15 itens de moldura
    no lugar das ~90 marcas do editor: Hierarchy, Scene, Inspector e o Play
    ficavam invisiveis pelo /state padrao.
    """
    if not hwnd:
        return len(elementos)          # sem janela para medir: criterio antigo
    r, p = RECT(), POINT(0, 0)
    if not (user32.GetClientRect(int(hwnd), byref(r)) and
            user32.ClientToScreen(int(hwnd), byref(p))):
        return len(elementos)
    dentro = 0
    for e in elementos:
        cx, cy = (e.get("center") or (None, None))[:2]
        if cx is not None and p.x <= cx < p.x + r.right and p.y <= cy < p.y + r.bottom:
            dentro += 1
    return dentro


def get_system_state(mode="auto", monitor="1", window_kw=None, groups=None, since=None):
    """Orquestrador híbrido: seleciona UIA se disponível, ou SoM visual para apps gráficos."""
    if window_kw:
        focus_window(window_kw)

    uia_elements = []
    aviso_uia = None
    janela = get_foreground_window_info() if mode == "auto" else {}
    chave = _chave_da_janela(janela)
    agora = time.time()
    moldura_em_cache = None
    if chave and UIA_SO_MOLDURA_TTL > 0:
        with _uia_so_moldura_lock:
            item = _uia_so_moldura.get(chave)
            if item and item[0] > agora:
                moldura_em_cache = item[1]
    if moldura_em_cache is not None:
        uia_elements = [dict(e) for e in moldura_em_cache]
    elif mode in ("auto", "uia"):
        uia_elements = inspect_window_uia(title_kw=window_kw)
        # Ler ANTES de qualquer copia: publish_state_snapshot faz list(...) e a
        # copia perde o atributo.
        aviso_uia = getattr(uia_elements, "truncation", None)

    # Se UIA retornar controles suficientes, usamos modo semântico (custo zero de imagem).
    # No auto, "suficientes" conta so o que esta dentro da area cliente: app de
    # interface propria (Unity, jogos, Electron sem acessibilidade) expoe a
    # moldura e mais nada. mode="uia" explicito continua com o criterio antigo.
    confia_uia = mode == "uia" or (
        mode == "auto" and moldura_em_cache is None and
        len(uia_elements) >= UIA_MIN_ELEMENTOS_CLIENTE and
        elementos_na_area_cliente(uia_elements, janela.get("hwnd"))
        >= UIA_MIN_ELEMENTOS_CLIENTE)
    if (mode == "auto" and chave and moldura_em_cache is None and not confia_uia
            and not aviso_uia and UIA_SO_MOLDURA_TTL > 0):
        # Varredura completa (sem truncation) que so achou moldura: guarda.
        with _uia_so_moldura_lock:
            for k in [k for k, v in _uia_so_moldura.items() if v[0] <= agora]:
                del _uia_so_moldura[k]
            _uia_so_moldura[chave] = (agora + UIA_SO_MOLDURA_TTL,
                                      [dict(e) for e in uia_elements])
    if len(uia_elements) >= 4 and mode != "som" and confia_uia:
        snapshot = publish_state_snapshot("uia", monitor, elements=uia_elements)
        saida_uia = {
            "ok": True,
            "mode": "uia",
            "count": len(uia_elements),
            "elements": uia_elements,
            "frame_id": snapshot["frame_id"],
            "timestamp": snapshot["timestamp"],
            "monitor": snapshot["monitor"],
            "window": snapshot["window"],
            "snapshot_ttl_seconds": SNAPSHOT_TTL_SECONDS,
        }
        if aviso_uia:
            saida_uia["truncation"] = aviso_uia
        return saida_uia
    else:
        # Consulta ao Oráculo de Invalidação DXGI (se habilitado)
        oracle = get_dxgi_oracle()
        if oracle and mode != "uia":
            cached = read_state_snapshot()
            wm = cached.get("dxgi_watermark")
            age = max(0.0, time.time() - float(cached.get("timestamp") or 0))
            if wm is not None and cached.get("marks") and age < SNAPSHOT_TTL_SECONDS:
                cached_win = cached.get("window") or {}
                curr_win = get_foreground_window_info() or {}
                if cached_win.get("hwnd") == curr_win.get("hwnd"):
                    win_rect = rect_da_janela(curr_win)
                    mudou, motivo, det = oracle.has_changed_since(wm, win_rect=win_rect)
                    if not mudou:
                        # EVIDÊNCIA TEMPORAL DE NÃO-MUDANÇA: Reutiliza snapshot em cache com segurança!
                        log.info("DXGI Oracle Cache Hit (%s): reutilizando snapshot em cache.", motivo)
                        cached_marks = list(cached.get("marks", []))
                        # O diff tambem vale aqui - e sobretudo aqui. Tela
                        # parada e o caso em que o snapshot incremental mede
                        # 90,8% de economia; montar a resposta na mao pulava o
                        # decidir_resposta_som e devolvia a lista inteira, sem
                        # snapshotToken, deixando o cliente sem base para o
                        # proximo diff. O oraculo economiza o OCR, o diff
                        # economiza o payload: sao camadas, nao alternativas.
                        # Coleta parcial em cache continua parcial: diff sobre
                        # ela descreveria remocoes que nunca houve.
                        truncation_cache = cached.get("truncation")
                        resposta_cache = decidir_resposta_som(
                            cached_marks, monitor,
                            (cached.get("window") or {}).get("hwnd"),
                            cached["frame_id"], since,
                            truncado=bool(truncation_cache))
                        saida_cached = {
                            "ok": True,
                            "mode": "som",
                            "count": len(cached_marks),
                            "groups": collect_groups(cached_marks),
                            "elements": list(cached.get("elements", [])),
                            "frame_id": cached["frame_id"],
                            "timestamp": cached["timestamp"],
                            "monitor": cached["monitor"],
                            "window": cached["window"],
                            "snapshot_ttl_seconds": SNAPSHOT_TTL_SECONDS,
                            "image_url": f"/som_frame?monitor={monitor}&t={int(cached['timestamp']*1000)}",
                            "cached_by_dxgi_oracle": True,
                            "dxgi_oracle_reason": motivo,
                        }
                        saida_cached.update(resposta_cache)
                        if resposta_cache.get("kind") == "full":
                            saida_cached["marks"] = cached_marks
                        if truncation_cache:
                            saida_cached["truncation"] = truncation_cache
                        return saida_cached

        # Fallback para Set-of-Mark OCR nativo (Full Snapshot)
        marks, annotated_jpeg = inspect_screen_som(
            monitor=monitor, draw_badges=True, groups=groups)
        aviso_som = getattr(marks, "truncation", None)
        detector_v2 = getattr(marks, "icon_detector", "v1") == "v2"
        if detector_v2 and uia_elements:
            # Prioridade acordada: UIA > texto > icone. Icone dentro de um
            # controle que a UIA ja nomeou (Minimizar, Fechar, itens de menu)
            # e o mesmo elemento duas vezes.
            caixas_uia = [e["bbox"] for e in uia_elements if e.get("bbox")]
            def _dentro_de_uia(m):
                cx, cy = m["center"]
                return any(bx <= cx <= bx + bw and by <= cy <= by + bh for bx, by, bw, bh in caixas_uia)
            antes = len(marks)
            marks[:] = [m for m in marks if m.get("type") == "text" or not _dentro_de_uia(m)]
            marks.icons_deduped_uia = antes - len(marks)
        snapshot = publish_state_snapshot(
            "som",
            monitor,
            elements=uia_elements,
            marks=marks,
            last_som_image=annotated_jpeg,
        )
        # A decisao vem antes de montar o payload: se for diff, "marks" sai de
        # cena e entram added/removed/moved/changed. Sem "since" a resposta e
        # exatamente a de antes, mais o snapshotToken - compativel por adicao.
        # v2: icone sai na forma compacta (<= 120 bytes). O diff tambem compara
        # nessa forma, para os dois lados da comparacao serem iguais; o cache
        # de estado (click_tag etc.) continua com a marca completa.
        marcas_saida = ([compactar_marca_de_icone(m) for m in marks]
                        if detector_v2 else marks)
        resposta = decidir_resposta_som(
            marcas_saida, monitor, (snapshot.get("window") or {}).get("hwnd"),
            snapshot["frame_id"], since, truncado=bool(aviso_som))
        saida_som = {
            "ok": True,
            "mode": "som",
            "count": len(marks),
            "groups": collect_groups(marks),
            "elements": uia_elements,
            "frame_id": snapshot["frame_id"],
            "timestamp": snapshot["timestamp"],
            "monitor": snapshot["monitor"],
            "window": snapshot["window"],
            "snapshot_ttl_seconds": SNAPSHOT_TTL_SECONDS,
            "image_url": f"/som_frame?monitor={monitor}&t={int(time.time()*1000)}"
        }
        saida_som.update(resposta)
        if resposta.get("kind") == "full":
            saida_som["marks"] = marcas_saida
        if aviso_som or aviso_uia:
            saida_som["truncation"] = aviso_som or aviso_uia
        saida_som["icon_detector"] = getattr(marks, "icon_detector", "v1")
        if SOM_ICON_DEBUG and getattr(marks, "icon_debug", None) is not None:
            saida_som["icon_debug"] = marks.icon_debug
        if exclusao_ativa():
            # O que esta sob uma janela excluida nao foi visto - dizer isso em
            # vez de devolver uma tela que parece completa.
            saida_som["excluded_marks"] = getattr(marks, "excluded_marks", 0)
            saida_som["occluded"] = regioes_ocluidas(get_monitor_geom(monitor))
        return saida_som


def _state_stability_signature(state):
    """Retorna somente dados determinísticos usados para comparar estados."""
    def item_signature(item, source):
        if source == "uia":
            keys = ("id", "name", "type", "center", "bbox", "control_type", "enabled")
        else:
            keys = ("tag", "text", "center", "bbox", "type")
        return {key: item.get(key) for key in keys if key in item}

    window = state.get("window") or {}
    return {
        "mode": state.get("mode"),
        "monitor": str(state.get("monitor")),
        "window": {
            "hwnd": window.get("hwnd"),
            "process": window.get("process"),
        },
        "elements": [item_signature(item, "uia") for item in state.get("elements", [])],
        "marks": [item_signature(item, "som") for item in state.get("marks", [])],
    }


def _states_are_stable(previous, current):
    previous_signature = _state_stability_signature(previous)
    current_signature = _state_stability_signature(current)
    return json.dumps(previous_signature, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        current_signature, ensure_ascii=False, sort_keys=True, default=str
    )


def get_stable_system_state(
    mode="auto",
    monitor="1",
    window_kw=None,
    stable_captures=DEFAULT_STABLE_CAPTURES,
    interval_ms=DEFAULT_STABLE_INTERVAL_MS,
    timeout_ms=DEFAULT_STABLE_TIMEOUT_MS,
    groups=None,
    since=None,
):
    """Captura estados locais até UIA/OCR/SoM permanecerem estáveis."""
    required = max(2, min(5, int(stable_captures)))
    interval_ms = max(20, min(500, int(interval_ms)))
    timeout_ms = max(100, min(10000, int(timeout_ms)))
    started = time.monotonic()
    previous = get_system_state(mode=mode, monitor=monitor, window_kw=window_kw,
                                groups=groups, since=since)
    stable_count = 1
    captures = 1

    while stable_count < required and (time.monotonic() - started) * 1000 < timeout_ms:
        remaining_ms = timeout_ms - int((time.monotonic() - started) * 1000)
        time.sleep(min(interval_ms, max(1, remaining_ms)) / 1000.0)
        # As capturas intermediarias sao so para aferir estabilidade; pedir
        # diff nelas consumiria o token antes da resposta que vai ao cliente.
        current = get_system_state(mode=mode, monitor=monitor, window_kw=window_kw,
                                   groups=groups)
        captures += 1
        if _states_are_stable(previous, current):
            stable_count += 1
        else:
            stable_count = 1
        previous = current

    previous["stability"] = {
        "stable": stable_count >= required,
        "captures": captures,
        "stable_captures": stable_count,
        "required_captures": required,
        "interval_ms": interval_ms,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "timeout_ms": timeout_ms,
    }
    if not previous["stability"]["stable"]:
        previous["error_code"] = "unstable_frame"
        previous["error"] = "A tela não permaneceu estável dentro do timeout informado."
    return previous

def _resolve_template_path(tpl_input):
    if not isinstance(tpl_input, str):
        return tpl_input
    if tpl_input.startswith("data:image") or len(tpl_input) > 260:
        return tpl_input
    if os.path.exists(tpl_input):
        return tpl_input
    candidates = [
        os.path.join(os.path.dirname(__file__), "templates", tpl_input),
        os.path.join(os.path.dirname(__file__), "templates", f"{tpl_input}.png"),
        os.path.join(r"C:\Users\Admin\Desktop\Desktop\templates", tpl_input),
        os.path.join(r"C:\Users\Admin\Desktop\Desktop\templates", f"{tpl_input}.png"),
        os.path.join(r"C:\Users\Admin\.gemini\antigravity-cli\brain\948ef8b0-92d2-44d1-893b-88fe596e8b8e\scratch", tpl_input),
        os.path.join(r"C:\Users\Admin\.gemini\antigravity-cli\brain\948ef8b0-92d2-44d1-893b-88fe596e8b8e\scratch", f"{tpl_input}.png"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return tpl_input

def locate_template_on_screen(template_path_or_bytes, confidence=0.8, monitor="1", scales=(0.8, 0.9, 1.0, 1.1, 1.25), window=None, hwnd=None):
    """
    Localiza template gráfico (ícones, botões sem texto) na tela ou dentro de uma janela alvo.
    Suporta multi-escala para compensar DPI scaling do Windows (100%, 125%, 150%).
    Suporta presets nomeados (ex: 'unity_play', 'unity_pause') e busca acelerada em ROI de janela.
    """
    ensure_desktop_access()
    try:
        resolved_tpl = _resolve_template_path(template_path_or_bytes)
        if isinstance(resolved_tpl, str):
            if resolved_tpl.startswith("data:image") or len(resolved_tpl) > 260:
                raw = base64.b64decode(resolved_tpl.split(",")[-1])
                tpl_img = Image.open(io.BytesIO(raw))
            else:
                tpl_img = Image.open(resolved_tpl)
        elif isinstance(resolved_tpl, (bytes, bytearray)):
            tpl_img = Image.open(io.BytesIO(resolved_tpl))
        elif hasattr(resolved_tpl, "convert"):
            tpl_img = resolved_tpl
        else:
            return {"ok": False, "found": False, "error": "Formato de template inválido"}

        screen_img = capture_pil_image(monitor=monitor, draw_cursor=False)
        ox, oy, mw, mh = get_monitor_geom(monitor)
        
        # Otimização: Se janela for informada, recorta ROI para acelerar busca em 100x
        roi_ox, roi_oy = ox, oy
        if window or hwnd:
            foc_ok, win_info = focus_window(title_kw=window, hwnd=hwnd)
            if foc_ok and win_info and win_info.get("hwnd"):
                rc = RECT()
                user32.GetClientRect(win_info["hwnd"], byref(rc))
                pt0 = POINT(0, 0)
                user32.ClientToScreen(win_info["hwnd"], byref(pt0))
                rx1 = max(0, pt0.x - ox)
                ry1 = max(0, pt0.y - oy)
                rx2 = min(screen_img.size[0], rx1 + max(1, rc.right - rc.left))
                ry2 = min(screen_img.size[1], ry1 + max(1, rc.bottom - rc.top))
                if rx2 > rx1 + 10 and ry2 > ry1 + 10:
                    screen_img = screen_img.crop((rx1, ry1, rx2, ry2))
                    roi_ox = ox + rx1
                    roi_oy = oy + ry1

        # Se OpenCV estiver disponível, executa matchTemplate multi-escala ultra-rápido
        if cv2 and np:
            screen_bgr = cv2.cvtColor(np.array(screen_img), cv2.COLOR_RGB2BGR)
            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
            tpl_bgr = cv2.cvtColor(np.array(tpl_img), cv2.COLOR_RGB2BGR)
            tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
            th, tw = tpl_gray.shape[:2]

            best_match = None
            for s in scales:
                if s == 1.0:
                    scaled_tpl = tpl_gray
                else:
                    nw = int(tw * s)
                    nh = int(th * s)
                    if nw >= screen_gray.shape[1] or nh >= screen_gray.shape[0] or nw < 5 or nh < 5:
                        continue
                    scaled_tpl = cv2.resize(tpl_gray, (nw, nh), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC)
                
                res = cv2.matchTemplate(screen_gray, scaled_tpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= confidence:
                    if best_match is None or max_val > best_match["confidence"]:
                        cur_h, cur_w = scaled_tpl.shape[:2]
                        best_match = {
                            "confidence": float(max_val),
                            "scale": s,
                            "box": {"left": int(max_loc[0]), "top": int(max_loc[1]), "width": int(cur_w), "height": int(cur_h)},
                            "center_x": int(roi_ox + max_loc[0] + cur_w // 2),
                            "center_y": int(roi_oy + max_loc[1] + cur_h // 2)
                        }

            if best_match:
                return {
                    "ok": True,
                    "found": True,
                    "confidence": round(best_match["confidence"], 4),
                    "matched_scale": best_match["scale"],
                    "x": best_match["center_x"],
                    "y": best_match["center_y"],
                    "center_x": best_match["center_x"],
                    "center_y": best_match["center_y"],
                    "rx": round((best_match["center_x"] - ox) / max(1, mw), 4),
                    "ry": round((best_match["center_y"] - oy) / max(1, mh), 4),
                    "box": best_match["box"]
                }
            return {"ok": True, "found": False, "confidence": 0.0}

        # Fallback para pyautogui.locate
        box = pyautogui.locate(tpl_img, screen_img, confidence=confidence)
        if box:
            center_x = roi_ox + box.left + box.width // 2
            center_y = roi_oy + box.top + box.height // 2
            return {
                "ok": True,
                "found": True,
                "x": center_x,
                "y": center_y,
                "center_x": center_x,
                "center_y": center_y,
                "rx": round((center_x - ox) / max(1, mw), 4),
                "ry": round((center_y - oy) / max(1, mh), 4),
                "box": {"left": box.left, "top": box.top, "width": box.width, "height": box.height}
            }
        return {"ok": True, "found": False}
    except Exception as e:
        return {"ok": False, "found": False, "error": str(e)}

# =====================================================================
# MÓDULO UI-TARS: ALGORITMO SMART RESIZE E PARSER DE AÇÕES AST
# =====================================================================
UITARS_IMAGE_FACTOR = 28
UITARS_MIN_PIXELS = 100 * 28 * 28
UITARS_MAX_PIXELS = 16384 * 28 * 28
UITARS_MAX_RATIO = 200

def round_by_factor(number: int, factor: int) -> int:
    return round(number / factor) * factor

def ceil_by_factor(number: int, factor: int) -> int:
    return math.ceil(number / factor) * factor

def floor_by_factor(number: int, factor: int) -> int:
    return math.floor(number / factor) * factor

def smart_resize(height: int, width: int, factor: int = UITARS_IMAGE_FACTOR,
                 min_pixels: int = UITARS_MIN_PIXELS, max_pixels: int = UITARS_MAX_PIXELS) -> tuple:
    """
    Algoritmo oficial ByteDance UI-TARS (Qwen2.5-VL ViT Patch 28).
    Garante que ambas as dimensões sejam divisíveis por 28 mantendo a proporção de tela.
    """
    if max(height, width) / max(1, min(height, width)) > UITARS_MAX_RATIO:
        pass
    h_bar = max(factor, round_by_factor(height, factor))
    w_bar = max(factor, round_by_factor(width, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = floor_by_factor(height / beta, factor)
        w_bar = floor_by_factor(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = ceil_by_factor(height * beta, factor)
        w_bar = ceil_by_factor(width * beta, factor)
    return h_bar, w_bar

def convert_point_to_coordinates(text: str) -> str:
    """Converte tags <point>x y</point> para tuplas (x,y)."""
    pattern = r"<point>(\d+)\s+(\d+)</point>"
    def replace_match(match):
        x, y = match.group(1), match.group(2)
        return f"({x},{y})"
    text = re.sub(r"\[EOS\]", "", text)
    return re.sub(pattern, replace_match, text).strip()

def parse_ast_action(action_str: str) -> dict:
    """
    Parser sintático AST nativo do UI-TARS para decompor expressões
    como click(point='(100,200)') ou drag(start_point='...', end_point='...')
    com segurança, sem regexes manuais frágeis.
    """
    try:
        clean_str = action_str.strip()
        if not clean_str:
            return None
        node = ast.parse(clean_str, mode='eval')
        if not isinstance(node, ast.Expression) or not isinstance(node.body, ast.Call):
            return None
        call = node.body
        func_name = call.func.id if isinstance(call.func, ast.Name) else (call.func.attr if isinstance(call.func, ast.Attribute) else None)
        kwargs = {}
        for kw in call.keywords:
            if isinstance(kw.value, ast.Constant):
                kwargs[kw.arg] = kw.value.value
            elif isinstance(kw.value, ast.Str):
                kwargs[kw.arg] = kw.value.s
            elif isinstance(kw.value, (ast.List, ast.Tuple)):
                kwargs[kw.arg] = [elt.value if isinstance(elt, ast.Constant) else str(elt) for elt in kw.value.elts]
            else:
                kwargs[kw.arg] = None
        return {'function': func_name, 'args': kwargs}
    except Exception:
        return None

def parse_uitars_output(text: str, origin_width: int, origin_height: int, model_type: str = "qwen25vl") -> list:
    """
    Converte resposta de saída do modelo UI-TARS para lista de ações com coordenadas desnormalizadas.
    """
    text = text.strip()
    if "<point>" in text:
        text = convert_point_to_coordinates(text)
    if "start_point=" in text:
        text = text.replace("start_point=", "start_box=")
    if "end_point=" in text:
        text = text.replace("end_point=", "end_box=")
    if "point=" in text:
        text = text.replace("point=", "start_box=")

    sr_height, sr_width = smart_resize(origin_height, origin_width)

    thought = None
    if "Thought:" in text:
        t_match = re.search(r"Thought:\s*(.+?)(?=\s*Action:|$)", text, re.DOTALL)
        if t_match:
            thought = t_match.group(1).strip()
    
    if "Action:" in text:
        action_part = text.split("Action:")[-1].strip()
    else:
        action_part = text

    raw_actions = [a.strip() for a in action_part.split(")\n\n") if a.strip()]
    parsed_list = []
    for raw in raw_actions:
        if not raw.endswith(")"):
            raw = raw + ")"
        ast_res = parse_ast_action(raw)
        if not ast_res or not ast_res.get("function"):
            continue
        fn = ast_res["function"]
        args = ast_res["args"]
        
        # Mapeia coordenadas start_box / end_box de volta para pixels reais
        coords_real = {}
        for box_key in ("start_box", "end_box"):
            if box_key in args and args[box_key]:
                val = str(args[box_key]).replace("(", "").replace(")", "").replace("<point>", "").replace("</point>", "").strip()
                nums = [float(n.strip()) for n in val.replace(",", " ").split() if n.strip()]
                if len(nums) >= 2:
                    raw_x, raw_y = nums[0], nums[1]
                    if model_type == "qwen25vl":
                        # UI-TARS no Qwen2.5-VL prediz no espaço redimensionado smart_resize
                        real_x = int((raw_x / sr_width) * origin_width)
                        real_y = int((raw_y / sr_height) * origin_height)
                    else:
                        # Coordenadas 0..1000 normalizadas
                        real_x = int((raw_x / 1000.0) * origin_width)
                        real_y = int((raw_y / 1000.0) * origin_height)
                    coords_real[box_key] = (real_x, real_y)
        
        parsed_list.append({
            "thought": thought,
            "action": fn,
            "args": args,
            "coords_real": coords_real,
            "raw": raw
        })
    return parsed_list

def execute_uitars_command(body):
    """
    Executa comandos no formato nativo UI-TARS (Thought + Action).
    """
    raw_text = str(body.get("command") or body.get("text") or body.get("raw") or body.get("action_str") or "").strip()
    mon = str(body.get("monitor", "1"))
    ox, oy, mw, mh = get_monitor_geom(mon)
    
    target_win = None
    if has_window_target(body):
        ok, target_win = focus_window(title_kw=body.get("window") or body.get("focus"),
                                      hwnd=body.get("hwnd"),
                                      process_name=body.get("process"))
        if ok and target_win:
            mw = target_win.get("width", mw)
            mh = target_win.get("height", mh)
            ox = target_win.get("left", ox)
            oy = target_win.get("top", oy)

    model_type = str(body.get("model_type", "qwen25vl"))
    parsed_actions = parse_uitars_output(raw_text, origin_width=mw, origin_height=mh, model_type=model_type)
    
    if not parsed_actions:
        return {"ok": False, "error": f"Não foi possível interpretar comando UI-TARS: '{raw_text}'"}

    results = []
    for item in parsed_actions:
        fn = item["action"]
        args = item["args"]
        coords = item["coords_real"]
        
        pt_start = coords.get("start_box")
        abs_x = (ox + pt_start[0]) if pt_start else None
        abs_y = (oy + pt_start[1]) if pt_start else None
        
        btn = str(args.get("button", "left")).lower()
        res = {"action": fn, "args": args}
        
        if fn in ("click", "click_coordinate"):
            if abs_x is not None and abs_y is not None:
                with input_action_lock:
                    win32_mouse_click(abs_x, abs_y, button=btn)
                res["ok"] = True
                res["x"] = abs_x
                res["y"] = abs_y
            else:
                res["ok"] = False
                res["error"] = "Coordenadas não fornecidas para click"

        elif fn in ("left_double", "double_click"):
            if abs_x is not None and abs_y is not None:
                with input_action_lock:
                    win32_mouse_click(abs_x, abs_y, button=btn, clicks=2)
                res["ok"] = True
                res["x"] = abs_x
                res["y"] = abs_y
            else:
                res["ok"] = False
                res["error"] = "Coordenadas não fornecidas para double_click"

        elif fn in ("right_single", "right_click"):
            if abs_x is not None and abs_y is not None:
                with input_action_lock:
                    win32_mouse_click(abs_x, abs_y, button="right", clicks=1)
                res["ok"] = True
                res["x"] = abs_x
                res["y"] = abs_y
            else:
                res["ok"] = False
                res["error"] = "Coordenadas não fornecidas para right_click"

        elif fn in ("drag", "drag_and_drop"):
            pt_end = coords.get("end_box")
            if abs_x is not None and abs_y is not None and pt_end:
                abs_x2 = ox + pt_end[0]
                abs_y2 = oy + pt_end[1]
                with input_action_lock:
                    win32_mouse_move(abs_x, abs_y)
                    time.sleep(0.04)
                    win32_mouse_down(button=btn, x=abs_x, y=abs_y)
                    time.sleep(0.04)
                    win32_mouse_smooth_move(abs_x2, abs_y2, duration=0.3)
                    time.sleep(0.04)
                    win32_mouse_up(button=btn, x=abs_x2, y=abs_y2)
                res["ok"] = True
                res["from"] = [abs_x, abs_y]
                res["to"] = [abs_x2, abs_y2]
            else:
                res["ok"] = False
                res["error"] = "Coordenadas de início e fim necessárias para drag"

        elif fn == "hotkey":
            key_combo = str(args.get("key") or args.get("keys") or "")
            keys = [k.strip().lower() for k in key_combo.replace("+", " ").split() if k.strip()]
            if keys:
                with input_action_lock:
                    pyautogui.hotkey(*keys)
                res["ok"] = True
                res["keys"] = keys
            else:
                res["ok"] = False
                res["error"] = "Nenhuma tecla especificada em hotkey"

        elif fn == "type":
            content = str(args.get("content") or args.get("text") or "")
            with input_action_lock:
                pyperclip.copy(content)
                time.sleep(0.02)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.02)
            res["ok"] = True
            res["content"] = content

        elif fn == "scroll":
            direction = str(args.get("direction", "down")).lower()
            dy = -300 if direction == "down" else (300 if direction == "up" else 0)
            dx = 300 if direction == "right" else (-300 if direction == "left" else 0)
            with input_action_lock:
                win32_mouse_scroll(dy=dy, dx=dx, x=abs_x, y=abs_y)
            res["ok"] = True
            res["direction"] = direction

        elif fn == "wait":
            sec = float(args.get("duration", args.get("seconds", 2.0)))
            time.sleep(sec)
            res["ok"] = True
            res["waited_seconds"] = sec

        elif fn == "finished":
            res["ok"] = True
            res["finished"] = True
            res["content"] = args.get("content", "")

        else:
            res["ok"] = False
            res["error"] = f"Ação UI-TARS não suportada: {fn}"

        results.append(res)

    all_ok = all(r.get("ok", False) for r in results)
    return {
        "ok": all_ok,
        "action": "uitars",
        "thought": parsed_actions[0].get("thought") if parsed_actions else None,
        "steps_count": len(results),
        "results": results
    }

def click_uia_element(name, control_type="ButtonControl", max_depth=8):
    if not uia:
        return {"ok": False, "error": "uiautomation não disponível"}
    try:
        ctrl_class = getattr(uia, control_type, uia.Control)
        ctrl = ctrl_class(searchDepth=max_depth, Name=name)
        if ctrl.Exists(2):
            rect = ctrl.BoundingRectangle
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            ctrl.Click()
            return {"ok": True, "action": "click_ui", "found": True, "name": name, "x": cx, "y": cy}
        return {"ok": False, "action": "click_ui", "found": False, "name": name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# =====================================================================
# ESCOPO DE DIALOGO ATIVO (modal)
# =====================================================================
# O grafo geometrico erra em layout sobreposto: um modal esta CONTIDO na
# janela de tras, mas nao pertence a ela. Adivinhar isso pela geometria e
# chute; o Win32 responde direto.
#
# GW_ENABLEDPOPUP devolve o popup HABILITADO que a janela possui - que e
# exatamente a definicao de modal ativo. Quando a janela de tras esta
# desabilitada pelo dialogo, e este handle que importa.

GW_ENABLEDPOPUP = 6


# Dica devolvida junto de ambiguous_target: o agente nao tem como adivinhar
# que existe um escopo se nunca ouviu falar dele.
AMBIGUOUS_HINT = ('repita com scope="active_modal" se o alvo esta num dialogo ou '
                  'menu recem-aberto (ou "active_dialog"/"context_menu" para ser '
                  'especifico), ou scope="window" para limitar a janela em foco')


def janela_de_dialogo_ativa(hwnd=None):
    """Devolve a descrição do diálogo modal ativo, ou None se não houver.

    Nao usa heuristica de tamanho nem de posicao: pergunta ao Win32 qual popup
    habilitado a janela possui.
    """
    ensure_desktop_access()
    base = int(hwnd or user32.GetForegroundWindow() or 0)
    if not base:
        return None
    try:
        popup = int(user32.GetWindow(base, GW_ENABLEDPOPUP) or 0)
    except Exception:
        return None
    if not popup or popup == base:
        return None
    return describe_window(popup)


# Menu de contexto do Windows. Medido no Unity 6.5: o menu de botao direito NAO
# e um popup pertencente a janela - `GetWindow(hwnd, GW_ENABLEDPOPUP)` devolve 0
# para ele. E uma janela da classe nativa "#32768" com Owner ZERO, criada na
# MESMA thread da janela que a abriu. Entao o caminho e a thread, nao o dono.
MENU_WINDOW_CLASS = "#32768"


def _classe_da_janela(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(hwnd, buf, 256):
        return ""
    return buf.value


def menu_de_contexto_ativo(hwnd=None):
    """Devolve o menu de contexto aberto pela janela, ou None.

    Varre so as janelas da thread dona do hwnd - nao o sistema inteiro -, o que
    mantem o custo perto de zero e evita pegar o menu de outro processo.
    """
    ensure_desktop_access()
    base = int(hwnd or user32.GetForegroundWindow() or 0)
    if not base:
        return None
    try:
        pid = wintypes.DWORD()
        tid = int(user32.GetWindowThreadProcessId(base, ctypes.byref(pid)) or 0)
    except Exception:
        return None
    if not tid:
        return None
    achados = []

    def _enum(h, _lparam):
        try:
            if user32.IsWindowVisible(h) and _classe_da_janela(h) == MENU_WINDOW_CLASS:
                achados.append(int(h))
        except Exception:
            pass
        return True

    try:
        user32.EnumThreadWindows(tid, WNDENUMPROC(_enum), 0)
    except Exception:
        return None
    if not achados:
        return None
    # Submenu abre por cima do menu pai e nasce depois: o ultimo da enumeracao e
    # o mais recente, e e nele que o alvo esta.
    return describe_window(achados[-1])


def _caixa(descricao):
    return [descricao["left"], descricao["top"], descricao["width"], descricao["height"]]


def _retangulo_do_escopo(escopo, snapshot):
    """Traduz scope em (região [x, y, w, h], motivo, o que foi resolvido)."""
    pedido = str(escopo or "").strip().casefold()
    janela = (snapshot.get("window") or {}) if snapshot else {}
    if pedido in ("", "all", "screen", "tela"):
        return None, None, "screen"
    if pedido in ("active_dialog", "dialog", "modal", "dialogo"):
        dialogo = janela_de_dialogo_ativa(janela.get("hwnd"))
        if not dialogo:
            return None, "sem_dialogo_ativo", None
        return _caixa(dialogo), None, "active_dialog"
    if pedido in ("context_menu", "menu", "popup_menu"):
        menu = menu_de_contexto_ativo(janela.get("hwnd"))
        if not menu:
            return None, "sem_menu_aberto", None
        return _caixa(menu), None, "context_menu"
    if pedido in ("active_modal", "topmost", "foreground"):
        # Escopo composto, para quem nao sabe se o que abriu foi menu ou dialogo.
        # O menu vem primeiro porque ele fica POR CIMA do dialogo quando os dois
        # existem. Qual dos dois entrou sai em `scope_resolved` - a resposta diz
        # o que foi usado em vez de deixar o agente supor.
        menu = menu_de_contexto_ativo(janela.get("hwnd"))
        if menu:
            return _caixa(menu), None, "context_menu"
        dialogo = janela_de_dialogo_ativa(janela.get("hwnd"))
        if dialogo:
            return _caixa(dialogo), None, "active_dialog"
        return None, "sem_modal_ativo", None
    if pedido in ("window", "janela"):
        if not janela.get("width"):
            return None, "sem_janela", None
        return _caixa(janela), None, "window"
    return None, "escopo_desconhecido", None


def resolver_escopo(body, snapshot):
    """Resolve `scope` do pedido em (região, erro).

    Devolve (None, None, None) quando nao ha escopo. Quando o escopo foi pedido mas
    nao existe - por exemplo `active_dialog` sem nenhum modal aberto - devolve
    um erro explicito em vez de deixar a busca cair na tela inteira.
    """
    pedido = body.get("scope")
    if not pedido:
        return None, None, None
    region, motivo, resolvido = _retangulo_do_escopo(pedido, snapshot)
    if motivo:
        return None, {
            "ok": False, "error_code": "scope_unavailable", "scope": pedido,
            "scope_reason": motivo,
            "error": f"Escopo '{pedido}' indisponivel ({motivo}); a busca foi abortada "
                     "para nao clicar fora dele.",
            "frame_id": (snapshot or {}).get("frame_id"),
        }, None
    return region, None, resolvido


def find_text_candidates(snapshot, target_text, exact=False, region=None,
                         element_type=None, scope=None):
    """Localiza texto com filtros determinísticos sem escolher o primeiro resultado.

    `scope="active_dialog"` restringe ao modal ativo. Quando o nome aparece nos
    dois lugares - "Salvar" no dialogo e "Salvar" na barra de tras -, sem escopo
    a busca acha dois e devolve ambiguous_target; com escopo acha um.
    """
    region = region if isinstance(region, (list, tuple)) and len(region) == 4 else None
    if region is None and scope:
        region, motivo, _resolvido = _retangulo_do_escopo(scope, snapshot)
        # Escopo pedido e nao resolvido nao vira busca na tela inteira: isso
        # redirecionaria o clique para um elemento fora do modal em silencio.
        if motivo:
            return []
    wanted_type = str(element_type or "").casefold()

    def varrer(normalizar, marca=None):
        needle = normalizar(target_text)
        if not needle:
            return []
        achados = []
        for source, items in (("som", snapshot.get("marks", [])),
                              ("uia", snapshot.get("elements", []))):
            for item in items:
                label = item.get("text") if source == "som" else item.get("name")
                normalized = normalizar(label)
                if not normalized or (normalized != needle if exact else needle not in normalized):
                    continue
                item_type = str(item.get("type", "")).casefold()
                if wanted_type and item_type != wanted_type:
                    continue
                cx, cy = item.get("center", (None, None))
                if cx is None:
                    continue
                if region:
                    rx, ry, rw, rh = (float(value) for value in region)
                    if not (rx <= cx <= rx + rw and ry <= cy <= ry + rh):
                        continue
                candidato = {
                    "source": source, "tag": item.get("tag"), "id": item.get("id"),
                    "text": label, "type": item.get("type"), "center": [cx, cy],
                    "bbox": item.get("bbox"),
                }
                if marca:
                    candidato["match"] = marca
                achados.append(candidato)
        return achados

    # Estrita primeiro: quando ela acha, o comportamento e exatamente o de antes.
    # A tolerante so entra quando a estrita nao acha nada, e o candidato sai
    # marcado - o agente sabe que o texto casou por normalizacao de OCR.
    return varrer(_normalizar_texto_estrito) or varrer(_normalizar_texto_ocr, "ocr_tolerante")


def _normalizar_texto_estrito(texto):
    return " ".join(str(texto or "").casefold().split())


# Confusoes de OCR MEDIDAS nesta maquina, e so elas: "TutorialInfo" lido como
# "Tutoriallnfo" (I -> l), e o placeholder lido ora "Pergunte ao ChatGPT", ora
# "+ pergunte ao ChatGPT" (simbolo vizinho grudado, caixa diferente). Nada de
# distancia de edicao: isso transformaria a busca em aproximada e o clique
# poderia cair em outro elemento.
_OCR_MESMA_LETRA = str.maketrans({"i": "l", "1": "l", "|": "l"})


def _normalizar_texto_ocr(texto):
    t = _normalizar_texto_estrito(texto).translate(_OCR_MESMA_LETRA)
    # Simbolo solto nas pontas ("+ ", "• ", "< ", " >"): so nas pontas, o meio
    # do texto nao muda. Sem letra ou digito nenhum, sobra vazio e nao casa.
    return re.sub(r"^[^\w]+|[^\w]+$", "", t).strip()



# =====================================================================
# DIARIO DE EXECUCAO (IDEIAS/02 - etapa 2 de 5)
# =====================================================================
# Toda acao bem-sucedida ja produz o que foi feito e onde. O diario apenas
# para de jogar isso fora: guarda a assinatura da tela ANTES, a acao, a
# assinatura DEPOIS e um recorte do alvo. E a materia-prima do compilador.
#
# Fica DESLIGADO por padrao: ligado, cada acao paga duas capturas (~10-30 ms).
# E barato perto dos 300-1000 ms de um SoM, mas nao deve ser cobrado de quem
# nao esta gravando.

JOURNAL_MAX_ENTRIES = max(10, int(os.environ.get("JOURNAL_MAX_ENTRIES", "300")))
JOURNAL_EVIDENCE_PX = max(16, int(os.environ.get("JOURNAL_EVIDENCE_PX", "72")))

# Acoes que nao mexem na tela nao entram no diario.
JOURNAL_SKIP_ACTIONS = {
    "state", "signature", "state_signature", "cursor", "windows",
    "crop", "roi", "find_template", "match_template",
    "journal", "journal_start", "journal_stop", "journal_clear",
    "skill_run", "run_skill", "skill", "skill_compile", "compile_skill",
    "skills", "skill_list", "skill_forget", "forget_skill",
    "som_exclude", "exclude_windows",
}

# Campos volumosos que nao fazem sentido guardar no diario.
JOURNAL_DROP_KEYS = {"base64", "image", "template_bytes", "frame_id"}

_journal_entries = collections.deque(maxlen=JOURNAL_MAX_ENTRIES)
_journal_lock = threading.RLock()
_journal_seq = 0
_journal_enabled = False
_journal_tls = threading.local()


def journal_set_enabled(enabled):
    """Liga/desliga a gravação. Desligar não apaga o que já foi gravado."""
    global _journal_enabled
    with _journal_lock:
        _journal_enabled = bool(enabled)
        return _journal_enabled


def journal_clear():
    with _journal_lock:
        count = len(_journal_entries)
        _journal_entries.clear()
        return count


def journal_entries(limit=None):
    """Cópia das entradas (sem os bytes da evidência, que não serializam)."""
    with _journal_lock:
        items = list(_journal_entries)
    if limit:
        items = items[-int(limit):]
    public = []
    for entry in items:
        copy = dict(entry)
        evidence = copy.get("evidence") or {}
        copy["evidence"] = {k: v for k, v in evidence.items() if k != "png"}
        copy["evidence"]["has_png"] = bool(evidence.get("png"))
        public.append(copy)
    return public


def _journal_sanitize(body):
    return {k: v for k, v in body.items()
            if k not in JOURNAL_DROP_KEYS and not isinstance(v, (bytes, bytearray))}


def _journal_capture(monitor="1", crop_at=None, samples=1):
    """Uma captura só, da qual saem a assinatura e (opcionalmente) a evidência.

    Capturar duas vezes para ter as duas coisas seria pagar o dobro por uma
    informação que está na mesma imagem.

    Com samples=2 tira uma segunda amostra logo em seguida e marca as celulas
    que mudaram sozinhas. Isso e o que da celula volatil para uma skill de UM
    passo so: a deteccao por pares (after[N] vs before[N+1]) precisa de dois
    passos e deixaria a trajetoria de passo unico sem protecao nenhuma contra
    relogio e spinner.
    """
    image, window, origin = _signature_source_image(monitor=monitor)
    signature = signature_from_image(image, window=window, monitor=monitor)
    if samples > 1:
        volatile = set()
        for _ in range(samples - 1):
            time.sleep(SIGNATURE_SAMPLE_INTERVAL / 2.0)
            extra, _w, _o = _signature_source_image(monitor=monitor)
            volatile |= _volatile_between(signature,
                                          signature_from_image(extra, window=window,
                                                               monitor=monitor))
        signature["volatile_cells"] = sorted(volatile)
    evidence = None
    if crop_at:
        half = JOURNAL_EVIDENCE_PX // 2
        # O clique vem em coordenada de TELA; a imagem pode estar cortada na area
        # cliente, entao a origem e quem faz a conversao.
        cx = int(crop_at[0]) - origin[0]
        cy = int(crop_at[1]) - origin[1]
        left = max(0, cx - half)
        top = max(0, cy - half)
        right = min(image.size[0], cx + half)
        bottom = min(image.size[1], cy + half)
        if right - left >= 8 and bottom - top >= 8:
            buf = io.BytesIO()
            image.crop((left, top, right, bottom)).save(buf, format="PNG")
            evidence = {
                "png": buf.getvalue(),
                "size": [right - left, bottom - top],
                "click_offset": [cx - left, cy - top],
                "screen_point": [int(crop_at[0]), int(crop_at[1])],
                "origin": [origin[0], origin[1]],
            }
    return signature, evidence


def _result_point(result):
    """Extrai o ponto clicado de um resultado, quando houver."""
    if not isinstance(result, dict):
        return None
    if result.get("x") is not None and result.get("y") is not None:
        return (result["x"], result["y"])
    return None


def journal_record(body, result, before, evidence, elapsed_ms):
    global _journal_seq
    after, _ = _journal_capture(monitor=str(body.get("monitor", "1")))
    with _journal_lock:
        _journal_seq += 1
        entry = {
            "seq": _journal_seq,
            "at": time.time(),
            "action": body.get("action"),
            "payload": _journal_sanitize(body),
            "result": {k: v for k, v in (result or {}).items()
                       if k not in ("results", "candidates", "live_state")},
            "before": before,
            "after": after,
            "evidence": evidence or {},
            "elapsed_ms": elapsed_ms,
        }
        _journal_entries.append(entry)
    return entry



# =====================================================================
# COMPILADOR DE SKILLS (IDEIAS/02 - etapa 3 de 5)
# =====================================================================
# Transforma um trecho do diario numa maquina de estados replayavel: cada passo
# carrega uma verificacao da tela (pre-condicao), a acao, a evidencia visual do
# alvo e o estado esperado depois (pos-condicao).

SKILLS_DIR = os.environ.get("SKILLS_DIR", os.path.join(os.getcwd(), "skills"))
SKILL_SCHEMA_VERSION = 1


def _skill_path(name):
    return os.path.join(SKILLS_DIR, f"{_safe_skill_name(name)}.json")


def _safe_skill_name(name):
    """Nome de skill vira nome de arquivo: nada de path traversal."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "").strip())
    cleaned = cleaned.strip("._") or "skill"
    return cleaned[:80]


def _volatile_between(sig_a, sig_b):
    """Celulas que mudaram entre duas capturas do MESMO estado."""
    cells_a = (sig_a or {}).get("cells") or []
    cells_b = (sig_b or {}).get("cells") or []
    if len(cells_a) != len(cells_b):
        return set()
    return {i for i, (a, b) in enumerate(zip(cells_a, cells_b)) if a != b}


def compile_skill_from_journal(name, entries=None, last=None, from_seq=None,
                               params=None, description=None):
    """Compila entradas do diário numa skill persistida em disco.

    As celulas volateis saem de graca do proprio diario: `after` do passo N e
    `before` do passo N+1 sao duas capturas do MESMO estado separadas por tempo
    real, entao o que difere entre elas e relogio/spinner, nao mudanca de tela.
    """
    with _journal_lock:
        raw = list(_journal_entries)
    if entries is None:
        entries = raw
        if from_seq is not None:
            entries = [e for e in entries if e["seq"] >= int(from_seq)]
        if last:
            entries = entries[-int(last):]
    if not entries:
        return {"ok": False, "error_code": "empty_journal",
                "error": "nao ha nada gravado para compilar. Ligue /journal/start antes."}

    # Uma celula que pisca em qualquer ponto da trajetoria pisca em todos.
    volatile_global = set()
    for entry in entries:
        volatile_global |= set((entry.get("before") or {}).get("volatile_cells") or [])
        volatile_global |= set((entry.get("after") or {}).get("volatile_cells") or [])
    for current, following in zip(entries, entries[1:]):
        volatile_global |= _volatile_between(current.get("after"), following.get("before"))

    first_window = (entries[0].get("before") or {}).get("window") or {}
    safe_name = _safe_skill_name(name)
    evidence_dir = os.path.join(SKILLS_DIR, safe_name)

    steps = []
    for index, entry in enumerate(entries, start=1):
        pre = dict(entry.get("before") or {})
        post = dict(entry.get("after") or {})
        pre["volatile_cells"] = sorted(volatile_global)
        post["volatile_cells"] = sorted(volatile_global)

        evidence = entry.get("evidence") or {}
        step = {
            "n": index,
            "precondition": pre,
            "action": _journal_sanitize(entry.get("payload") or {}),
            "postcondition": post,
            "recorded_ms": entry.get("elapsed_ms"),
        }
        if evidence.get("png"):
            os.makedirs(evidence_dir, exist_ok=True)
            rel = os.path.join(safe_name, f"s{index:02d}.png")
            with open(os.path.join(SKILLS_DIR, rel), "wb") as handle:
                handle.write(evidence["png"])
            step["evidence"] = {
                "template": rel.replace("\\", "/"),
                "size": evidence.get("size"),
                "click_offset": evidence.get("click_offset"),
                "screen_point": evidence.get("screen_point"),
                "origin": evidence.get("origin"),
            }
        steps.append(step)

    skill = {
        "schema": SKILL_SCHEMA_VERSION,
        "skill": safe_name,
        "description": description or "",
        "version": 1,
        "app": {
            # O PROCESSO e a porteira dura. O titulo nao entra como regex por
            # padrao porque titulo de app real muda o tempo todo (nome da cena,
            # arquivo aberto, asterisco de nao-salvo): travar no titulo exato
            # faria a skill nunca mais casar. Fica gravado so como referencia, e
            # quem quiser pode preencher title_regex a mao depois.
            "process": first_window.get("process"),
            "title_regex": None,
            "title_sample": first_window.get("title"),
            "client_size": first_window.get("client_size"),
        },
        "params": list(params or []),
        "compiled_from": {
            "at": time.time(),
            "first_seq": entries[0].get("seq"),
            "last_seq": entries[-1].get("seq"),
            "volatile_cells": sorted(volatile_global),
        },
        "stats": {"runs": 0, "ok": 0, "diverged": 0, "last_ok": None, "avg_ms": None},
        "steps": steps,
    }

    existing = load_skill(safe_name)
    if existing:
        skill["version"] = int(existing.get("version", 1)) + 1
        skill["stats"] = existing.get("stats", skill["stats"])
    save_skill(skill)
    return {"ok": True, "action": "skill_compile", "skill": safe_name,
            "version": skill["version"], "steps": len(steps),
            "volatile_cells": sorted(volatile_global), "path": _skill_path(safe_name)}


def save_skill(skill):
    os.makedirs(SKILLS_DIR, exist_ok=True)
    path = _skill_path(skill["skill"])
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(skill, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def load_skill(name):
    try:
        with open(_skill_path(name), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def list_skills():
    out = []
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return out
    for filename in names:
        if not filename.endswith(".json"):
            continue
        skill = load_skill(filename[:-5])
        if not skill:
            continue
        out.append({
            "skill": skill.get("skill"),
            "version": skill.get("version"),
            "steps": len(skill.get("steps") or []),
            "app": skill.get("app"),
            "stats": skill.get("stats"),
            "description": skill.get("description"),
        })
    return out


def forget_skill(name):
    safe = _safe_skill_name(name)
    removed = False
    try:
        os.remove(_skill_path(safe))
        removed = True
    except OSError:
        pass
    directory = os.path.join(SKILLS_DIR, safe)
    if os.path.isdir(directory):
        for filename in os.listdir(directory):
            try:
                os.remove(os.path.join(directory, filename))
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass
    return removed



# =====================================================================
# REPLAYER DE SKILLS (IDEIAS/02 - etapa 4 de 5)
# =====================================================================
# Caminha a maquina de estados conferindo a tela ANTES de cada acao. Isso e o
# que separa isto de uma macro de RPA: macro dispara os passos gravados as
# cegas; aqui, se a tela nao for a esperada, o replay PARA e devolve o controle
# junto com o estado ao vivo, sem refazer o prefixo que ja funcionou.

SKILL_STEP_TIMEOUT_MS = max(500, int(os.environ.get("SKILL_STEP_TIMEOUT_MS", "4000")))
# Re-mira (IBTR): confianca ALTA de proposito. Re-mirar e agir sobre uma tela que
# ja reprovou na pre-condicao, entao so vale quando o alvo e reconhecido sem
# duvida; a pos-condicao continua sendo a rede de seguranca.
SKILL_REAIM_CONFIDENCE = max(0.5, min(1.0, float(os.environ.get("SKILL_REAIM_CONFIDENCE", "0.9"))))
SKILL_POLL_MS = max(20, int(os.environ.get("SKILL_POLL_MS", "80")))


def _apply_params(value, params):
    """Substitui {{nome}} pelos parâmetros, recursivamente."""
    if isinstance(value, str):
        for key, replacement in (params or {}).items():
            value = value.replace("{{" + str(key) + "}}", str(replacement))
        return value
    if isinstance(value, dict):
        return {k: _apply_params(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_apply_params(v, params) for v in value]
    return value


def _app_gate(skill, current_signature):
    """O processo é porteira: outro app em foco nunca 'quase bate'."""
    expected = (skill.get("app") or {})
    window = (current_signature or {}).get("window") or {}
    if expected.get("process") and window.get("process") and \
            expected["process"] != window["process"]:
        return {"ok": False, "reason": "process_mismatch",
                "expected_process": expected["process"],
                "current_process": window.get("process")}
    pattern = expected.get("title_regex")
    if pattern:
        try:
            if not re.search(pattern, str(window.get("title") or "")):
                return {"ok": False, "reason": "title_mismatch",
                        "title_regex": pattern, "current_title": window.get("title")}
        except re.error:
            pass
    return {"ok": True}


def _wait_for_postcondition(step, monitor, tolerance, timeout_ms):
    """Espera o estado esperado aparecer; devolve (bateu, ultima comparacao)."""
    expected = step.get("postcondition")
    if not expected or not expected.get("cells"):
        return True, None
    deadline = time.time() + max(0.05, timeout_ms / 1000.0)
    comparison = None
    while True:
        current = compute_state_signature(monitor=monitor, samples=1)
        comparison = signature_distance(expected, current, tolerance=tolerance)
        if comparison.get("match"):
            return True, comparison
        if time.time() >= deadline:
            return False, comparison
        time.sleep(SKILL_POLL_MS / 1000.0)


def _skill_bump_stats(skill, *, ok, elapsed_ms):
    stats = skill.setdefault("stats", {})
    stats["runs"] = int(stats.get("runs") or 0) + 1
    if ok:
        stats["ok"] = int(stats.get("ok") or 0) + 1
        stats["last_ok"] = time.time()
        previous = stats.get("avg_ms")
        stats["avg_ms"] = elapsed_ms if not previous else int((previous * 3 + elapsed_ms) / 4)
    else:
        stats["diverged"] = int(stats.get("diverged") or 0) + 1
    try:
        save_skill(skill)
    except OSError:
        pass


def _diverged(skill, step_number, reason, detail, completed, started,
              monitor, want_live_state=True):
    """Para o replay e devolve o que o agente precisa para assumir dali."""
    _replay_em_curso.ativo = False
    payload = {
        "ok": False,
        "action": "skill_run",
        "error_code": "skill_diverged",
        "skill": skill.get("skill"),
        "version": skill.get("version"),
        "failed_at_step": step_number,
        "reason": reason,
        "detail": detail,
        "completed_steps": completed,
        "total_steps": len(skill.get("steps") or []),
        "elapsed_ms": int((time.time() - started) * 1000),
        "resume_hint": {"action": "skill_run", "name": skill.get("skill"),
                        "from_step": step_number},
    }
    if want_live_state:
        # O prefixo que funcionou nao e refeito: o agente continua do passo que
        # falhou, com a tela ja parseada na mao.
        try:
            marks, _ = inspect_screen_som(monitor=monitor, draw_badges=False)
            snapshot = publish_state_snapshot("som", monitor, marks=marks)
            payload["live_state"] = {
                "frame_id": snapshot["frame_id"],
                "marks": snapshot["marks"],
                "window": snapshot.get("window"),
            }
        except Exception as exc:
            payload["live_state_error"] = str(exc)
    return payload


def run_skill(name, params=None, tolerance=None, from_step=1, monitor=None,
              live_state=True, dry_run=False, reaim=True, reaim_confidence=None):
    """Replaya uma skill conferindo a tela antes de cada passo."""
    skill = load_skill(name)
    if not skill:
        return {"ok": False, "action": "skill_run", "error_code": "skill_not_found",
                "error": f"skill '{name}' nao existe", "skill": name}

    steps = skill.get("steps") or []
    if not steps:
        return {"ok": False, "action": "skill_run", "error_code": "skill_empty",
                "skill": skill.get("skill")}

    monitor = str(monitor or (steps[0].get("precondition") or {}).get("monitor") or "1")
    started = time.time()
    _replay_em_curso.ativo = True
    executed = []
    completed = 0

    for step in steps:
        number = int(step.get("n") or 0)
        if number < int(from_step or 1):
            continue

        current = compute_state_signature(monitor=monitor, samples=1)
        gate = _app_gate(skill, current)
        if not gate["ok"]:
            _skill_bump_stats(skill, ok=False, elapsed_ms=0)
            return _diverged(skill, number, gate["reason"], gate, completed,
                             started, monitor, live_state)

        comparison = signature_distance(step.get("precondition"), current,
                                        tolerance=tolerance)
        if not comparison.get("match"):
            outcome = _handle_precondition_miss(
                skill, step, current, comparison, monitor, tolerance,
                reaim=reaim, reaim_confidence=reaim_confidence)
            if not outcome.get("recovered"):
                _skill_bump_stats(skill, ok=False, elapsed_ms=0)
                return _diverged(skill, number, "precondition_mismatch",
                                 {"comparison": comparison,
                                  "recovery": outcome.get("detail")},
                                 completed, started, monitor, live_state)
            step = outcome["step"]
            step["_reaim_detail"] = outcome.get("detail")

        action = _apply_params(dict(step.get("action") or {}), params)
        action.setdefault("monitor", monitor)
        if dry_run:
            result = {"ok": True, "dry_run": True}
        else:
            result = _execute_system_action(action)
        if not isinstance(result, dict) or not result.get("ok"):
            _skill_bump_stats(skill, ok=False, elapsed_ms=0)
            return _diverged(skill, number, "action_failed", {"result": result},
                             completed, started, monitor, live_state)

        timeout_ms = int(step.get("postcondition", {}).get("timeout_ms")
                         or max(SKILL_STEP_TIMEOUT_MS,
                                int(step.get("recorded_ms") or 0) * 4))
        if not dry_run:
            reached, post_comparison = _wait_for_postcondition(
                step, monitor, tolerance, timeout_ms)
            if not reached:
                _skill_bump_stats(skill, ok=False, elapsed_ms=0)
                return _diverged(skill, number, "postcondition_timeout",
                                 {"comparison": post_comparison,
                                  "timeout_ms": timeout_ms},
                                 completed, started, monitor, live_state)

        completed += 1
        entry = {"n": number, "action": action.get("action"),
                 "reaimed": bool(step.get("_reaimed"))}
        if step.get("_reaimed"):
            entry["reaim"] = step.get("_reaim_detail")
        executed.append(entry)

    _replay_em_curso.ativo = False
    elapsed_ms = int((time.time() - started) * 1000)
    _skill_bump_stats(skill, ok=True, elapsed_ms=elapsed_ms)
    return {"ok": True, "action": "skill_run", "skill": skill.get("skill"),
            "version": skill.get("version"), "steps_executed": completed,
            "total_steps": len(steps), "elapsed_ms": elapsed_ms,
            "steps": executed, "dry_run": bool(dry_run)}


def _reaim_step(step, monitor, confidence=None):
    """Re-localiza o alvo do passo pela evidência visual (IBTR).

    Quando a janela muda de posicao, de tamanho ou de monitor, a coordenada
    gravada aponta para o lugar errado e a assinatura inteira muda junto. Mas o
    ELEMENTO continua na tela, so que noutro ponto: procurar o recorte do alvo
    resolve o caso mais comum de quebra sem precisar de IA nenhuma.
    """
    evidence = step.get("evidence") or {}
    template = evidence.get("template")
    if not template:
        # So passo com evidencia e re-mirável, e evidencia so existe quando a
        # acao teve um ponto. Teclado e atalho nao tem o que re-mirar.
        return {"recovered": False, "strategy": "none", "reason": "passo sem evidencia visual"}

    path = os.path.join(SKILLS_DIR, template)
    if not os.path.isfile(path):
        return {"recovered": False, "strategy": "template", "reason": "arquivo de evidencia ausente",
                "template": template}

    threshold = SKILL_REAIM_CONFIDENCE if confidence is None else max(0.5, min(1.0, float(confidence)))
    try:
        found = locate_template_on_screen(path, confidence=threshold, monitor=monitor)
    except Exception as exc:
        return {"recovered": False, "strategy": "template", "error": str(exc)}

    if not found or not found.get("found"):
        return {"recovered": False, "strategy": "template", "reason": "alvo nao encontrado na tela",
                "threshold": threshold}

    # O centro do template NAO e o ponto clicado: o recorte foi feito em volta do
    # clique, entao click_offset diz onde dentro do recorte o clique caiu.
    size = evidence.get("size") or [0, 0]
    offset = evidence.get("click_offset") or [size[0] // 2, size[1] // 2]
    new_x = int(found["x"] - size[0] // 2 + offset[0])
    new_y = int(found["y"] - size[1] // 2 + offset[1])

    original = dict(step.get("action") or {})
    antigo = evidence.get("screen_point") or []
    # Vira clique absoluto no ponto reencontrado: dispensa o OCR que o
    # click_text faria e nao depende mais da coordenada velha.
    acao = {
        "action": "double_click" if original.get("action") == "double_click" else (
            "right_click" if original.get("action") == "right_click" else "click"),
        "x": new_x, "y": new_y, "absolute": True,
        "button": original.get("button", "left"),
        "monitor": monitor,
    }
    if original.get("clicks"):
        acao["clicks"] = original["clicks"]

    deslocamento = None
    if len(antigo) == 2:
        deslocamento = [new_x - int(antigo[0]), new_y - int(antigo[1])]

    novo_passo = dict(step)
    novo_passo["action"] = acao
    novo_passo["_reaimed"] = True
    return {"recovered": True, "strategy": "template", "step": novo_passo,
            "confidence": found.get("confidence"), "moved_by": deslocamento,
            "from": antigo or None, "to": [new_x, new_y]}


def _handle_precondition_miss(skill, step, current, comparison, monitor, tolerance,
                              reaim=True, reaim_confidence=None):
    """A pré-condição reprovou: tenta re-mirar antes de declarar divergência."""
    if not reaim:
        return {"recovered": False, "detail": {"strategy": "disabled"}}
    outcome = _reaim_step(step, monitor, confidence=reaim_confidence)
    if outcome.get("recovered"):
        return {"recovered": True, "step": outcome.pop("step"), "detail": outcome}
    return {"recovered": False, "detail": outcome}


held_keys = set()

# Executor central de comandos do sistema com suporte determinístico

# =====================================================================
# DESFECHO DA ACAO: ENVIADA x VERIFICADA
# =====================================================================
# "ok": true sempre quis dizer "o SendInput saiu", nunca "o clique conseguiu
# alguma coisa" - e o agente nao tinha como distinguir. Botao inerte aceita
# clique sem fazer nada; botao que troca de nome (Open -> Close) ou some nao
# e motivo para repetir. Sem essa distincao o agente cai no laco de retry, e
# repetir um clique que JA SAIU pode agir duas vezes.
#
# "ok" continua significando o que sempre significou. Os campos novos so
# acrescentam o que faltava.
#
#   actionDispatched  true  entrada foi enviada ao sistema
#                     false nada foi enviado - repetir e seguro
#                     null  nao da para saber - trate como enviada
#   outcomeVerified   true  o servidor OBSERVOU o efeito esperado
#   outcome           verified | dispatched | not_dispatched
#                     | observation | unknown

# Acoes que enviam entrada. As demais so consultam.
ACOES_DE_ENTRADA = {
    "click", "double_click", "right_click", "move", "hover", "scroll", "drag",
    "drag_and_drop", "drag_to", "mousedown", "mouse_down", "down", "mouseup",
    "mouse_up", "up", "type", "paste", "key", "hotkey", "keydown", "keyup",
    "release_keys", "release_all", "click_id", "click_tag", "click_text",
    "type_into", "click_ui", "click_and_verify", "find_and_click",
    "click_template", "template", "uitars", "act_uitars", "focus", "pipeline",
    "wait_text", "wait_for", "wait", "skill_run", "run_skill", "skill",
    "hover_probe", "probe",
}

# Acoes que so retornam ok DEPOIS de observar o efeito esperado.
ACOES_VERIFICADAS = {
    "click_and_verify", "wait_text", "wait_for", "wait",
    "skill_run", "run_skill", "skill",
}

# Quando a acao falha, estes sinais dizem que a entrada JA TINHA SAIDO. Sao o
# caso perigoso: o agente nao pode repetir achando que nada aconteceu.
_SINAIS_DE_ENVIO = ("click_result", "clicked", "completed_steps", "probed")
_ERROS_APOS_ENVIO = ("verification_timeout", "postcondition_timeout", "window_changed")


def _anotar_desfecho(body, resultado, entrada_antes=None):
    """Acrescenta actionDispatched / outcomeVerified / outcome ao resultado.

    Fica no dispatcher, e nao nos ~27 pontos de retorno, porque a classificacao
    depende so do nome da acao e do formato do resultado.
    """
    if not isinstance(resultado, dict) or "outcome" in resultado:
        return resultado

    acao = str(body.get("action") or resultado.get("action") or "")
    resultado = dict(resultado)

    if acao not in ACOES_DE_ENTRADA:
        resultado["actionDispatched"] = False
        resultado["outcomeVerified"] = False
        resultado["outcome"] = "observation"
        return resultado

    if resultado.get("ok"):
        verificada = acao in ACOES_VERIFICADAS or resultado.get("verified") is True
        resultado["actionDispatched"] = True
        resultado["outcomeVerified"] = bool(verificada)
        resultado["outcome"] = "verified" if verificada else "dispatched"
        return resultado

    # Falhou. A pergunta que importa para o agente e: da para repetir?
    # O contador e a resposta MEDIDA; os sinais no resultado so complementam
    # (uma acao aninhada pode ter enviado antes de o contador ser lido).
    medido = None if entrada_antes is None else (_entrada_enviada > entrada_antes)
    saiu = bool(medido)
    saiu = saiu or any(resultado.get(campo) for campo in _SINAIS_DE_ENVIO)
    saiu = saiu or resultado.get("error_code") in _ERROS_APOS_ENVIO
    if saiu:
        resultado["actionDispatched"] = True
        resultado["outcome"] = "dispatched"
    elif medido is False:
        # O contador nao se moveu: nada saiu, e repetir e seguro. Isto e
        # medicao, nao leitura da forma do resultado.
        resultado["actionDispatched"] = False
        resultado["outcome"] = "not_dispatched"
    else:
        # Sem medicao disponivel. Nao inventar "false": afirmar que nada saiu
        # quando talvez tenha saido e o erro que faz o agente clicar duas vezes.
        resultado["actionDispatched"] = None
        resultado["outcome"] = "unknown"
    resultado["outcomeVerified"] = False
    return resultado


# =====================================================================
# GUARDA DE REPETICAO
# =====================================================================
# Cada acao aqui e independente: o verification_timeout olha UMA chamada. Um
# agente autonomo pode reenviar o mesmo clique num botao morto cinquenta vezes
# sem que nada perceba, porque nenhuma das cinquenta chamadas sabe das outras.
#
# A regra e a mais estreita que resolve isso: recusar so quando a MESMA acao ja
# falhou E a tela nao mudou desde entao. Repetir clique com a tela diferente e
# legitimo (o botao mudou de estado); repetir uma acao que deu certo tambem
# (clicar "+" tres vezes). So o par "mesma acao, mesma tela, falhou antes" e
# que nao tem como dar outro resultado.
#
# O custo fica no caminho patologico: o hash da acao nao toca em I/O, e a
# captura de assinatura so acontece quando a acao JA esta na lista de falhadas.
# Acao inedita - o caso normal - nao paga nada.

REPEAT_GUARD = os.environ.get("REPEAT_GUARD", "1").strip().lower() not in ("0", "false", "no")
REPEAT_GUARD_MAX = max(8, int(os.environ.get("REPEAT_GUARD_MAX", "64")))

# Fora do hash: mudam a cada captura ou sao volume, nao identidade da acao.
# Acoes cujo proposito e justamente esperar por uma mudanca futura, ou
# restaurar seguranca. Recusa-las por "a tela nao mudou AGORA" inverte o
# sentido delas: um wait_text que estourou em 5 s pode acertar em 7 s, e
# release_keys tem que funcionar sempre, inclusive - sobretudo - repetido.
ACOES_FORA_DA_GUARDA = {
    "wait", "wait_for", "wait_text", "release_keys", "release_all",
    "hover_probe", "probe",
}

_CHAVES_FORA_DO_HASH = {
    "frame_id", "since", "base64", "image", "template_bytes",
    "journal", "allow_repeat", "verify_timeout_ms",
}

_acoes_falhadas = collections.OrderedDict()
_acoes_falhadas_lock = threading.RLock()


def hash_da_acao(body):
    """Identidade da acao = a sua forma executavel, nao o rotulo."""
    limpo = {k: v for k, v in body.items() if k not in _CHAVES_FORA_DO_HASH}
    material = json.dumps(limpo, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(material.encode("utf-8", "replace")).hexdigest()[:16]


def _tela_mudou_desde(registro):
    """Comparação por células, ignorando as voláteis da falha anterior.

    Uma amostra so: as celulas volateis (relogio, contador de frame da Unity)
    ja foram identificadas na captura da falha, quando o custo de varias
    amostras foi pago uma vez.
    """
    try:
        atual = compute_state_signature(monitor=registro.get("monitor", "1"), samples=1)
    except Exception:
        return True                      # na duvida, deixa tentar
    antes = registro.get("cells") or []
    agora = atual.get("cells") or []
    if not antes or len(antes) != len(agora):
        return True
    volateis = set(registro.get("volatile_cells") or [])
    return any(a != b for i, (a, b) in enumerate(zip(antes, agora)) if i not in volateis)


def guarda_de_repeticao(body):
    """Devolve (chave, recusa|None) antes de executar."""
    if not REPEAT_GUARD or body.get("allow_repeat"):
        return None, None
    acao = str(body.get("action") or "")
    if acao not in ACOES_DE_ENTRADA or acao in ACOES_FORA_DA_GUARDA:
        return None, None
    chave = hash_da_acao(body)
    with _acoes_falhadas_lock:
        registro = _acoes_falhadas.get(chave)
        registro = dict(registro) if registro else None
    if not registro:
        return chave, None
    if _tela_mudou_desde(registro):
        # A tela e outra: a acao pode muito bem dar certo agora.
        with _acoes_falhadas_lock:
            _acoes_falhadas.pop(chave, None)
        return chave, None
    with _acoes_falhadas_lock:
        vivo = _acoes_falhadas.get(chave)
        if vivo:
            vivo["tentativas"] = vivo.get("tentativas", 1) + 1
            tentativas = vivo["tentativas"]
        else:
            tentativas = registro.get("tentativas", 1) + 1
    return chave, {
        "ok": False,
        "error_code": "repeated_failed_action",
        "action": body.get("action"),
        "actionDispatched": False,
        "outcomeVerified": False,
        "outcome": "not_dispatched",
        "attempts": tentativas,
        "previous_error_code": registro.get("error_code"),
        "previous_error": registro.get("error"),
        "error": ("Esta acao ja falhou e a tela nao mudou desde entao; repetir "
                  "daria o mesmo resultado."),
        "hint": ("mude alguma coisa antes de repetir (outro alvo, scope, "
                 'auto_refresh) ou envie "allow_repeat": true se a repeticao '
                 "for mesmo intencional"),
    }


def registrar_desfecho_da_acao(chave, body, resultado):
    """Guarda a falha com a assinatura da tela; sucesso limpa o registro."""
    if not chave or not isinstance(resultado, dict):
        return
    if resultado.get("ok"):
        with _acoes_falhadas_lock:
            _acoes_falhadas.pop(chave, None)
        return
    if resultado.get("error_code") == "repeated_failed_action":
        return                           # a propria recusa nao e uma falha nova
    monitor = str(body.get("monitor", "1"))
    try:
        # Aqui vale pagar varias amostras: e o unico ponto em que as celulas
        # volateis sao identificadas, e elas e que evitam falso "mudou".
        assinatura = compute_state_signature(monitor=monitor)
    except Exception:
        return
    with _acoes_falhadas_lock:
        anterior = _acoes_falhadas.pop(chave, None) or {}
        _acoes_falhadas[chave] = {
            "monitor": monitor,
            "cells": assinatura.get("cells"),
            "volatile_cells": assinatura.get("volatile_cells"),
            "error_code": resultado.get("error_code"),
            "error": str(resultado.get("error") or "")[:200],
            "tentativas": anterior.get("tentativas", 0) + 1,
            "quando": time.time(),
        }
        while len(_acoes_falhadas) > REPEAT_GUARD_MAX:
            _acoes_falhadas.popitem(last=False)


def execute_system_action(body):
    """Executa a ação e, se o diário estiver ligado, registra o que aconteceu.

    A gravação envolve o dispatcher em vez de ser espalhada pelos ~100 pontos de
    retorno dele. O contador por thread evita gravar duas vezes a mesma coisa
    quando uma ação chama outra (type_into -> click_id, pipeline -> passos).
    """
    depth = getattr(_journal_tls, "depth", 0)
    _journal_tls.depth = depth + 1
    try:
        entrada_antes = _entrada_enviada
        chave_repeticao = None
        if depth == 0:
            chave_repeticao, recusa = guarda_de_repeticao(body)
            if recusa:
                return recusa
            recusa = _recusa_teclado_em_janela_excluida(body)
            if recusa:
                return recusa
        oracle = get_dxgi_oracle() if depth == 0 else None
        wm_pre = oracle.get_watermark() if oracle else None
        target_win_rect = None
        if oracle and depth == 0:
            target_win_rect = rect_da_janela(get_foreground_window_info())

        if depth == 0 and "precondition" in body:
            from outcome_verifier import OutcomeVerifier
            _verifier = OutcomeVerifier()
            ok_pre, reason_pre, ev_pre = _verifier.evaluate_precondition(body["precondition"])
            if not ok_pre:
                return {
                    "ok": False,
                    "error_code": "precondition_failed",
                    "reason": reason_pre,
                    "actionDispatched": False,
                    "outcomeVerified": False,
                    "outcome": {
                        "status": "failed",
                        "verified": False,
                        "verifier": "precondition",
                        "reason": reason_pre,
                        "evidence": ev_pre
                    }
                }

        try:
            resultado = _executar_com_diario(body, depth)
        except (CoordenadaForaDaTela, AlvoOcluido) as erro:
            # Recusada antes de qualquer SendInput: o contador nao se moveu e
            # _anotar_desfecho mede actionDispatched=False sozinho.
            resultado = erro.resposta(body.get("action"))

        # Settle pós-ação focado na janela-alvo com motor de quiescência:
        # Aguarda estabilização da janela-alvo sem ser enganado por atividade concorrente externa
        # nem sair prematuramente antes da conclusão da animação.
        if oracle and wm_pre is not None and isinstance(resultado, dict):
            mudou, motivo, _ = oracle.wait_for_settle(
                wm_pre, win_rect=target_win_rect,
                quiescence_window_ms=40, min_observe_ms=30, max_timeout_ms=300
            )
            if mudou:
                oracle.last_invalidation_reason = f"post_action_{motivo}"

        # actionDispatched e MEDIDO pelo contador de entrada, com ou sem expect.
        # O bloco do expect abaixo so sobrescreve o desfecho (o que foi
        # verificado), nunca o despacho (o que foi enviado) - sao perguntas
        # diferentes, e supor a segunda foi justamente o bug que o contador
        # veio resolver.
        if depth == 0:
            resultado = _anotar_desfecho(body, resultado, entrada_antes)

        # Se expect foi fornecido pelo chamador, executa o OutcomeVerifier oficial
        if depth == 0 and "expect" in body and isinstance(resultado, dict):
            from outcome_verifier import OutcomeVerifier
            _verifier = OutcomeVerifier()
            target_meta = {
                "id": body.get("id") or body.get("click_id"),
                "tag": body.get("tag") or body.get("click_tag"),
                "text": body.get("text") or body.get("target") or body.get("click_text"),
                "center": [resultado.get("x"), resultado.get("y")] if ("x" in resultado and "y" in resultado) else None
            }
            v_res = _verifier.verify_outcome(
                body["expect"],
                action_target_meta=target_meta,
                timeout_ms=body.get("verify_timeout_ms", 1500)
            )
            resultado["outcome"] = v_res
            resultado["outcomeVerified"] = (v_res.get("status") == "verified")

        if depth == 0:
            registrar_desfecho_da_acao(chave_repeticao, body, resultado)
        return resultado
    finally:
        _journal_tls.depth = depth


def _executar_com_diario(body, depth):
    """Parte da gravação, separada para o dispatcher ter uma saída só."""
    if depth > 0 or not _journal_enabled:
        return _execute_system_action(body)
    if body.get("action") in JOURNAL_SKIP_ACTIONS or body.get("journal") is False:
        return _execute_system_action(body)

    monitor = str(body.get("monitor", "1"))
    before, _ = _journal_capture(monitor=monitor, samples=2)
    started = time.time()
    result = _execute_system_action(body)
    elapsed_ms = int((time.time() - started) * 1000)
    if isinstance(result, dict) and result.get("ok"):
        # So o que deu certo vira materia-prima: o compilador nunca deve
        # aprender um caminho que falhou.
        point = _result_point(result)
        evidence = None
        if point:
            _, evidence = _journal_capture(monitor=monitor, crop_at=point)
        result = dict(result)
        entry = journal_record(body, result, before, evidence, elapsed_ms)
        result["journal_seq"] = entry["seq"]
    return result


def _execute_system_action(body):
    ensure_desktop_access()
    action = body.get("action", "")
    btn = body.get("button", "left")
    
    # A CLI envia window/hwnd/process como None em quase toda acao. Testar so a
    # presenca da chave disparava um focus_window vazio, que enumerava todas as
    # janelas (com OpenProcess em cada uma) para no fim nao focar nada.
    if has_window_target(body):
        focus_window(
            title_kw=body.get("focus") or body.get("window") or body.get("title"),
            hwnd=body.get("hwnd"),
            process_name=body.get("process") or body.get("process_name")
        )

    # Executa um clique e somente confirma sucesso depois de observar o efeito.
    if action == "click_and_verify":
        click_request = dict(body.get("click") or {})
        expected = dict(body.get("expect") or {})
        expected_text = str(expected.get("text") or "").strip()
        if not click_request.get("action") or not expected_text:
            return {"ok": False, "error_code": "invalid_verification",
                    "error": "click.action e expect.text sao obrigatorios"}
        click_result = execute_system_action(click_request)
        if not click_result.get("ok"):
            return {"ok": False, "action": action, "stage": "click",
                    "error": "O clique inicial falhou", "details": click_result,
                    "error_code": click_result.get("error_code")}

        timeout = max(0.1, float(body.get("timeout", 15.0)))
        interval = max(0.05, float(body.get("interval", 0.2)))
        stable_required = max(1, int(body.get("stable_captures", 2)))
        expected_state = str(expected.get("state", "present")).casefold()
        monitor = str(body.get("monitor", "1"))
        started = time.time()
        stable_count = 0
        attempts = 0
        last_candidates = []
        verified_window = (read_state_snapshot().get("window") or {}).copy()
        while time.time() - started <= timeout:
            if body.get("verify_window", True) and verified_window.get("hwnd"):
                active_window = get_foreground_window_info() or {}
                if int(active_window.get("hwnd") or 0) != int(verified_window["hwnd"]):
                    return {"ok": False, "action": action, "verified": False,
                            "error_code": "window_changed", "stage": "verification",
                            "error": "A janela ativa mudou durante a verificacao.",
                            "expected_window": verified_window, "active_window": active_window,
                            "click_result": click_result}
            attempts += 1
            marks, _ = inspect_screen_som(monitor=monitor, draw_badges=False)
            snapshot = publish_state_snapshot("som", monitor, marks=marks)
            last_candidates = find_text_candidates(
                snapshot, expected_text, exact=bool(expected.get("exact", False)),
                region=expected.get("region"),
                element_type=expected.get("element_type") or expected.get("type_filter"),
                scope=expected.get("scope") or body.get("scope"))
            satisfied = (not last_candidates) if expected_state in ("absent", "hidden", "gone") else bool(last_candidates)
            stable_count = stable_count + 1 if satisfied else 0
            if stable_count >= stable_required:
                return {"ok": True, "action": action, "verified": True,
                        "expected_state": expected_state, "candidates": last_candidates,
                        "attempts": attempts, "elapsed": round(time.time() - started, 3),
                        "click_result": click_result, "frame_id": snapshot["frame_id"]}
            time.sleep(min(0.8, interval * (1.35 ** max(0, attempts - 1))))
        return {"ok": False, "action": action, "verified": False,
                "error_code": "verification_timeout", "expected_state": expected_state,
                "error": f"Resultado esperado '{expected_text}' nao foi confirmado.",
                "candidates": last_candidates, "attempts": attempts,
                "elapsed": round(time.time() - started, 3), "click_result": click_result}

    # 1. Clique determinístico por ID (UIA)
    if action == "click_id" or "click_id" in body:
        target_id = int(body.get("click_id") or body.get("id", 0))
        offset_x = int(body.get("offset_x", 0))
        offset_y = int(body.get("offset_y", 0))
        snapshot = read_state_snapshot()
        frame_error = validate_frame_reference(body, snapshot)
        if frame_error:
            return frame_error
        for elem in snapshot.get("elements", []):
            if elem["id"] == target_id:
                cx, cy = elem["center"]
                with input_action_lock:
                    win32_mouse_click(cx + offset_x, cy + offset_y, button=btn)
                return {
                    "ok": True, "action": "click_id", "id": target_id,
                    "name": elem.get("name"), "x": cx + offset_x, "y": cy + offset_y,
                    "frame_id": snapshot.get("frame_id"),
                    "legacy_frame_reference": not bool(body.get("frame_id")),
                }
        return {"ok": False, "error": f"Element ID {target_id} não encontrado. Chame /state primeiro."}

    # 2. Clique determinístico por Tag numérica (Set-of-Mark OCR)
    if action == "click_tag" or "click_tag" in body:
        target_tag = int(body.get("click_tag") or body.get("tag", 0))
        offset_x = int(body.get("offset_x", 0))
        offset_y = int(body.get("offset_y", 0))
        snapshot = read_state_snapshot()
        frame_error = validate_frame_reference(body, snapshot)
        if frame_error:
            return frame_error
        for mark in snapshot.get("marks", []):
            if mark["tag"] == target_tag:
                cx, cy = mark["center"]
                with input_action_lock:
                    win32_mouse_click(cx + offset_x, cy + offset_y, button=btn)
                return {
                    "ok": True, "action": "click_tag", "tag": target_tag,
                    "text": mark.get("text"), "x": cx + offset_x, "y": cy + offset_y,
                    "frame_id": snapshot.get("frame_id"),
                    "legacy_frame_reference": not bool(body.get("frame_id")),
                }
        # Nao esta visivel. Se estiver em carencia, a tag existe e a identidade
        # foi preservada - mas identidade nao e permissao de clique. Recaptura e
        # so clica se ela voltar compativel; nunca no ultimo lugar conhecido.
        mon_snap = str(snapshot.get("monitor") or body.get("monitor", "1"))
        hwnd_snap = int((snapshot.get("window") or {}).get("hwnd") or 0)
        retido = marca_em_quarentena(mon_snap, hwnd_snap, target_tag)
        if retido:
            fresh, _ = inspect_screen_som(monitor=mon_snap, draw_badges=False)
            novo = publish_state_snapshot("som", mon_snap, marks=fresh)
            atual = next((m for m in fresh if m.get("tag") == target_tag), None)
            if atual and _reaparecimento_compativel(retido["mark"], atual):
                cx, cy = atual["center"]
                with input_action_lock:
                    win32_mouse_click(cx + offset_x, cy + offset_y, button=btn)
                return {
                    "ok": True, "action": "click_tag", "tag": target_tag,
                    "text": atual.get("text"), "x": cx + offset_x, "y": cy + offset_y,
                    "revalidated": True, "quarantine_misses": retido.get("faltas"),
                    "frame_id": novo.get("frame_id"),
                }
            return {
                "ok": False, "error_code": "target_not_currently_visible",
                "action": "click_tag", "tag": target_tag,
                "quarantine_misses": retido.get("faltas"),
                "reappeared": bool(atual),
                "reason": "identidade_incompativel" if atual else "ainda_ausente",
                "error": (f"Tag {target_tag} esta em carencia: a identidade foi "
                          "preservada, mas o elemento nao esta visivel agora. "
                          "Nao clicamos no ultimo lugar conhecido."),
                "last_known": retido.get("mark"),
                "frame_id": novo.get("frame_id"),
            }
        return {"ok": False, "error": f"Tag {target_tag} não encontrada no cache SoM. Chame /state ou /som primeiro."}

    # 3. Clique inteligente por Texto (Varre SoM, depois UIA, depois OCR ao vivo)
    if action == "click_text" or "click_text" in body:
        target_text = str(body.get("click_text") or body.get("text", "")).strip().lower()
        offset_x = int(body.get("offset_x", 0))
        offset_y = int(body.get("offset_y", 0))
        auto_refresh = bool(body.get("auto_refresh", False) or body.get("refresh", False))
        mon = str(body.get("monitor", "1"))

        if not auto_refresh:
            snapshot = read_state_snapshot()
            frame_error = validate_frame_reference(body, snapshot)
            if frame_error:
                return frame_error
            region_escopo, erro_escopo, escopo_usado = resolver_escopo(body, snapshot)
            if erro_escopo:
                return erro_escopo
            candidates = find_text_candidates(
                snapshot, target_text, exact=bool(body.get("exact", False)),
                region=body.get("region") or region_escopo,
                element_type=body.get("element_type") or body.get("type_filter"))
            if len(candidates) > 1:
                return {"ok": False, "error_code": "ambiguous_target",
                        "error": f"Texto '{target_text}' encontrou {len(candidates)} candidatos.",
                        "candidate_count": len(candidates), "candidates": candidates,
                        "hint": AMBIGUOUS_HINT, "scope": body.get("scope"),
                        "scope_resolved": escopo_usado,
                        "frame_id": snapshot.get("frame_id")}
            if len(candidates) == 1:
                candidate = candidates[0]
                cx, cy = candidate["center"]
                real_cx, real_cy = cx + offset_x, cy + offset_y
                with input_action_lock:
                    win32_mouse_click(real_cx, real_cy, button=btn)
                return {"ok": True, "action": "click_text", "found_in": candidate["source"],
                        "text": candidate["text"], "tag": candidate["tag"], "id": candidate["id"],
                        "x": real_cx, "y": real_cy, "offset": [offset_x, offset_y],
                        "scope_resolved": escopo_usado,
                        "frame_id": snapshot.get("frame_id")}

        # Busca ao vivo via OCR rápido e atualiza cache
        marks, _ = inspect_screen_som(monitor=mon, draw_badges=False)
        snapshot = publish_state_snapshot("som", mon, marks=marks)
        region_escopo, erro_escopo, escopo_usado = resolver_escopo(body, snapshot)
        if erro_escopo:
            return erro_escopo
        candidates = find_text_candidates(
            snapshot, target_text, exact=bool(body.get("exact", False)),
            region=body.get("region") or region_escopo,
            element_type=body.get("element_type") or body.get("type_filter"))
        if len(candidates) > 1:
            return {"ok": False, "error_code": "ambiguous_target",
                    "error": f"Texto '{target_text}' encontrou {len(candidates)} candidatos.",
                    "candidate_count": len(candidates), "candidates": candidates,
                    "hint": AMBIGUOUS_HINT, "scope": body.get("scope"),
                    "scope_resolved": escopo_usado,
                    "frame_id": snapshot.get("frame_id")}
        if len(candidates) == 1:
            candidate = candidates[0]
            cx, cy = candidate["center"]
            real_cx, real_cy = cx + offset_x, cy + offset_y
            with input_action_lock:
                win32_mouse_click(real_cx, real_cy, button=btn)
            return {"ok": True, "action": "click_text", "found_in": "live_ocr",
                    "text": candidate["text"], "tag": candidate["tag"],
                    "x": real_cx, "y": real_cy, "offset": [offset_x, offset_y],
                    "scope_resolved": escopo_usado,
                    "frame_id": snapshot.get("frame_id")}
        return {"ok": False, "error": f"Texto '{target_text}' não encontrado na tela"}

    # 4. Digitação com foco automático
    if action == "type_into":
        click_res = None
        frame_options = {
            "frame_id": body.get("frame_id"),
            "verify_window": body.get("verify_window", True),
        }
        if "id" in body:
            click_res = execute_system_action({"action": "click_id", "id": body["id"], "offset_x": body.get("offset_x", 0), "offset_y": body.get("offset_y", 0), **frame_options})
        elif "tag" in body:
            click_res = execute_system_action({"action": "click_tag", "tag": body["tag"], "offset_x": body.get("offset_x", 0), "offset_y": body.get("offset_y", 0), **frame_options})
        elif "target" in body:
            click_res = execute_system_action({
                "action": "click_text",
                "text": body["target"],
                "offset_x": body.get("offset_x", 0),
                "offset_y": body.get("offset_y", 0),
                "auto_refresh": body.get("auto_refresh", False),
                "monitor": body.get("monitor", "1"),
                "exact": body.get("exact", False),
                "region": body.get("region"),
                "element_type": body.get("element_type") or body.get("type_filter"),
                "scope": body.get("scope"),
                **frame_options,
            })
        if click_res and click_res.get("ok"):
            with input_action_lock:
                time.sleep(0.05)
                text = str(body.get("text", ""))
                pyperclip.copy(text)
                time.sleep(0.03)
                pyautogui.hotkey("ctrl", "v")
                if body.get("enter", False):
                    time.sleep(0.03)
                    pyautogui.press("enter")
            return {"ok": True, "action": "type_into", "clicked": click_res, "text": text}
        failure = {"ok": False, "error": "Falha ao focar elemento alvo para digitação", "details": click_res}
        if click_res and click_res.get("error_code"):
            failure["error_code"] = click_res["error_code"]
        return failure

    # 5. Ações de Mouse convencionais e contínuas (clicar, segurar e soltar)
    if action in ("click", "double_click", "right_click", "move", "mousedown", "mouse_down", "down", "mouseup", "mouse_up", "up"):
        real_x, real_y, mon = coords_from_payload(body)
        
        if action == "click":
            clicks = int(body.get("clicks", 1))
            with input_action_lock:
                win32_mouse_click(real_x, real_y, button=btn, clicks=clicks)
            return {"ok": True, "action": "click", "x": real_x, "y": real_y, "clicks": clicks}

        elif action == "double_click":
            with input_action_lock:
                win32_mouse_click(real_x, real_y, button=btn, clicks=2)
            return {"ok": True, "action": "double_click", "x": real_x, "y": real_y}

        elif action == "right_click":
            with input_action_lock:
                win32_mouse_click(real_x, real_y, button="right", clicks=1)
            return {"ok": True, "action": "right_click", "x": real_x, "y": real_y}

        elif action in ("mousedown", "mouse_down", "down"):
            with input_action_lock:
                win32_mouse_down(button=btn, x=real_x, y=real_y)
            return {"ok": True, "action": "mousedown", "button": btn, "x": real_x, "y": real_y}

        elif action in ("mouseup", "mouse_up", "up"):
            with input_action_lock:
                win32_mouse_up(button=btn, x=real_x, y=real_y)
            return {"ok": True, "action": "mouseup", "button": btn, "x": real_x, "y": real_y}

        elif action == "move":
            # Suporte especial para Unity: delta relativo puro (dx, dy) para rotacionar câmera 3D na Scene View
            is_relative = bool(body.get("relative", False) or ("dx" in body and "dy" in body and "x" not in body and "rx" not in body))
            if is_relative:
                dx = int(body.get("dx", body.get("x", 0)))
                dy = int(body.get("dy", body.get("y", 0)))
                with input_action_lock:
                    win32_mouse_move(dx, dy, relative=True)
                return {"ok": True, "action": "move", "relative": True, "dx": dx, "dy": dy}

            duration = float(body.get("duration", 0.01))
            smooth = bool(body.get("smooth", False))
            tween_name = str(body.get("tween", "easeInOutQuad"))
            tween_fn = getattr(pyautogui, tween_name, pyautogui.easeInOutQuad) if smooth else None
            if smooth and duration <= 0.02:
                duration = 0.25
            with input_action_lock:
                if smooth and tween_fn:
                    win32_mouse_smooth_move(real_x, real_y, duration=duration, tween_fn=tween_fn)
                else:
                    win32_mouse_move(real_x, real_y, relative=False)
            return {"ok": True, "action": "move", "x": real_x, "y": real_y, "duration": duration, "smooth": smooth}

    elif action in ("drag", "drag_and_drop", "drag_to"):
        mon = str(body.get("monitor", "1"))
        ox, oy, mw, mh = get_monitor_geom(mon)
        x1, y1, x2, y2 = None, None, None, None

        # Suporte a foco de janela com mapeamento da área cliente
        target_hwnd = None
        if "focus" in body or "window" in body or "process" in body or "hwnd" in body:
            kw = body.get("focus") or body.get("window")
            ok, target_win = focus_window(title_kw=kw, hwnd=body.get("hwnd"), process_name=body.get("process"))
            if ok and target_win:
                target_hwnd = target_win.get("hwnd")

        # 1. Arrasto baseado em Templates (OpenCV)
        if "from_template" in body and "to_template" in body:
            conf = float(body.get("confidence", 0.75))
            m1 = locate_template_on_screen(body["from_template"], confidence=conf, monitor=mon, hwnd=target_hwnd)
            m2 = locate_template_on_screen(body["to_template"], confidence=conf, monitor=mon, hwnd=target_hwnd)
            if not m1.get("found"):
                return {"ok": False, "error": f"Template de origem '{body['from_template']}' não localizado na tela"}
            if not m2.get("found"):
                return {"ok": False, "error": f"Template de destino '{body['to_template']}' não localizado na tela"}
            x1, y1 = m1["center_x"], m1["center_y"]
            x2, y2 = m2["center_x"], m2["center_y"]

        # 2. Arrasto baseado em texto SoM/OCR (ex: arrastar de "Elemento A" para "Elemento B")
        elif "from_text" in body and "to_text" in body:
            from_target = str(body["from_text"]).lower().strip()
            to_target = str(body["to_text"]).lower().strip()
            marks, _ = inspect_screen_som(monitor=mon, draw_badges=False)
            for m in marks:
                txt = m.get("text", "").lower()
                if from_target in txt and x1 is None:
                    x1, y1 = m["center"]
                if to_target in txt and x2 is None:
                    x2, y2 = m["center"]
            if x1 is None or x2 is None:
                return {"ok": False, "error": f"Não foi possível localizar ambos os textos de origem ('{from_target}') e destino ('{to_target}') na tela"}

        # 3. Arrasto com coordenadas de Área Cliente da janela (ClientToScreen)
        elif target_hwnd and ("client_x1" in body or "cx1" in body or "client_rx1" in body or "crx1" in body):
            rc = RECT()
            user32.GetClientRect(target_hwnd, byref(rc))
            cw = max(1, rc.right - rc.left)
            ch = max(1, rc.bottom - rc.top)
            if "client_rx1" in body or "crx1" in body:
                cx1 = int(float(body.get("client_rx1", body.get("crx1", 0.0))) * cw)
                cy1 = int(float(body.get("client_ry1", body.get("cry1", 0.0))) * ch)
                cx2 = int(float(body.get("client_rx2", body.get("crx2", 0.0))) * cw)
                cy2 = int(float(body.get("client_ry2", body.get("cry2", 0.0))) * ch)
            else:
                cx1 = int(body.get("client_x1", body.get("cx1", 0)))
                cy1 = int(body.get("client_y1", body.get("cy1", 0)))
                cx2 = int(body.get("client_x2", body.get("cx2", cx1)))
                cy2 = int(body.get("client_y2", body.get("cy2", cy1)))
            pt1 = POINT(cx1, cy1)
            pt2 = POINT(cx2, cy2)
            user32.ClientToScreen(target_hwnd, byref(pt1))
            user32.ClientToScreen(target_hwnd, byref(pt2))
            x1, y1 = pt1.x, pt1.y
            x2, y2 = pt2.x, pt2.y

        elif "rx1" in body and "ry1" in body:
            if target_hwnd:
                rc = RECT()
                user32.GetClientRect(target_hwnd, byref(rc))
                pt0 = POINT(0, 0)
                user32.ClientToScreen(target_hwnd, byref(pt0))
                cw = max(1, rc.right - rc.left)
                ch = max(1, rc.bottom - rc.top)
                x1 = pt0.x + int(float(body["rx1"]) * cw)
                y1 = pt0.y + int(float(body["ry1"]) * ch)
                x2 = pt0.x + int(float(body.get("rx2", body["rx1"])) * cw)
                y2 = pt0.y + int(float(body.get("ry2", body["ry1"])) * ch)
            else:
                x1 = ox + int(float(body["rx1"]) * mw)
                y1 = oy + int(float(body["ry1"]) * mh)
                x2 = ox + int(float(body.get("rx2", body["rx1"])) * mw)
                y2 = oy + int(float(body.get("ry2", body["ry1"])) * mh)
        else:
            raw_x1 = int(body.get("x1", 0))
            raw_y1 = int(body.get("y1", 0))
            raw_x2 = int(body.get("x2", raw_x1))
            raw_y2 = int(body.get("y2", raw_y1))
            if body.get("absolute", False) or "monitor" not in body or target_hwnd:
                x1, y1 = raw_x1, raw_y1
                x2, y2 = raw_x2, raw_y2
            else:
                x1, y1 = ox + raw_x1, oy + raw_y1
                x2, y2 = ox + raw_x2, oy + raw_y2

        duration = float(body.get("duration", 0.35))
        # As duas pontas validadas ANTES de qualquer entrada: recusar o destino
        # depois do mouse_down deixaria o botao pressionado.
        exigir_ponto_na_tela(x1, y1)
        exigir_ponto_na_tela(x2, y2)
        exigir_nao_ocluido(x1, y1, body.get("hwnd"))
        exigir_nao_ocluido(x2, y2, body.get("hwnd"))   # soltar em cima tambem nao
        with input_action_lock:
            win32_mouse_move(x1, y1)
            time.sleep(0.05)
            win32_mouse_down(button=btn, x=x1, y=y1)
            solto = False
            try:
                time.sleep(0.05)
                # Threshold de ativação OLE: mover 5 pixels na direção do destino para iniciar drag no Windows/Unity
                dx = 5 if x2 >= x1 else -5
                dy = 5 if y2 >= y1 else -5
                win32_mouse_move(x1 + dx, y1 + dy)
                time.sleep(0.03)
                # Arrasto suave até o destino
                win32_mouse_smooth_move(x2, y2, duration=duration)
                # Dwell time crucial: aguardar 120ms no destino para o drop target (Unity/Windows) registrar o hover
                time.sleep(0.12)
                win32_mouse_up(button=btn, x=x2, y=y2)
                solto = True
                time.sleep(0.03)
            finally:
                # Qualquer excecao no meio do arraste solta o botao onde o
                # cursor estiver: botao preso vira arraste involuntario do
                # proximo movimento, inclusive o do usuario.
                if not solto:
                    win32_mouse_up(button=btn)
        return {"ok": True, "action": "drag", "from": [x1, y1], "to": [x2, y2], "duration": duration, "button": btn}

    elif action == "scroll":
        real_x, real_y, mon = coords_from_payload(body)
        dy = int(body.get("dy", 0))
        dx = int(body.get("dx", 0))
        with input_action_lock:
            win32_mouse_scroll(dy=dy, dx=dx, x=real_x, y=real_y)
        return {"ok": True, "action": "scroll", "dy": dy, "dx": dx}

    # Todas as acoes de teclado abaixo passam pelo kb_dispatch (worker dedicado),
    # que as serializa numa fila. A thread HTTP continua esperando o resultado (a
    # chamada e sincrona), mas deixa de SEGURAR o input_action_lock durante uma
    # digitacao longa, entao um clique vindo de outra requisicao nao fica preso
    # atras dela. A excecao e release_keys, que e valvula de emergencia e nao
    # pode ficar atras da acao que porventura travou.
    elif action in ("type", "paste"):
        text = str(body.get("text", ""))
        use_paste = body.get("paste", True) or (action == "paste")
        enter_after = body.get("enter", False)
        interval = float(body.get("interval", 0.01))

        def _do_type():
            with input_action_lock:
                if use_paste:
                    pyperclip.copy(text)
                    time.sleep(0.02)
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.02)
                else:
                    pyautogui.typewrite(text, interval=interval)
                if enter_after:
                    time.sleep(0.03)
                    pyautogui.press("enter")
            return {"ok": True, "action": "type", "length": len(text), "paste": use_paste}

        return kb_dispatch(_do_type)

    elif action == "key":
        key = body.get("key", "")
        times = int(body.get("times", 1))

        def _do_key():
            with input_action_lock:
                for _ in range(times):
                    pyautogui.press(key)
            return {"ok": True, "action": "key", "key": key, "times": times}

        return kb_dispatch(_do_key)

    elif action == "hotkey":
        keys = body.get("keys") or body.get("key", "")
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.replace("+", " ").split()]

        def _do_hotkey():
            with input_action_lock:
                pyautogui.hotkey(*keys)
            return {"ok": True, "action": "hotkey", "keys": keys}

        return kb_dispatch(_do_hotkey)

    elif action == "keydown":
        key = body.get("key", "")

        def _do_keydown():
            with input_action_lock:
                if key not in held_keys:
                    held_keys.add(key)
                    pyautogui.keyDown(key)
            return {"ok": True, "action": "keydown", "key": key}

        return kb_dispatch(_do_keydown)

    elif action == "keyup":
        key = body.get("key", "")

        def _do_keyup():
            with input_action_lock:
                held_keys.discard(key)
                pyautogui.keyUp(key)
            return {"ok": True, "action": "keyup", "key": key}

        return kb_dispatch(_do_keyup)

    elif action in ("release_keys", "release_all"):
        # A UI web manda keydown/keyup por evento: se a aba perde o foco entre os
        # dois, a tecla fica fisicamente presa na maquina e nao havia como soltar.
        #
        # Esta e a valvula de emergencia, entao ela NAO entra na fila do worker e
        # NAO espera o lock indefinidamente: se a fila ou o lock estiverem presos
        # por uma acao de teclado travada, enfileirar aqui seria esperar
        # justamente pelo culpado. Soltar tecla e idempotente, entao no pior caso
        # forcamos sem o lock.
        acquired = input_action_lock.acquire(timeout=2.0)
        _marcar_entrada_enviada()
        try:
            released = []
            for k in sorted(held_keys.copy()):
                try:
                    pyautogui.keyUp(k)
                    released.append(k)
                except Exception:
                    pass
            held_keys.clear()
            for k in ("ctrl", "alt", "shift", "win", "ctrlleft", "altleft",
                      "shiftleft", "winleft", "ctrlright", "altright", "shiftright"):
                try:
                    pyautogui.keyUp(k)
                except Exception:
                    pass
        finally:
            if acquired:
                input_action_lock.release()
        return {"ok": True, "action": "release_keys", "released": released,
                "forced": not acquired}



    elif action == "focus":
        hwnd = body.get("hwnd")
        proc = body.get("process") or body.get("process_name")
        title_kw = body.get("title") or body.get("window") or body.get("focus")
        ok, info = focus_window(title_kw=title_kw, hwnd=hwnd, process_name=proc)
        return {"ok": ok, "window": info}

    elif action in ("wait_text", "wait_for", "wait"):
        target_text = str(body.get("text") or body.get("target") or body.get("wait_text") or "").strip().lower()
        if not target_text:
            return {"ok": False, "error": "Parâmetro 'text' ou 'target' é obrigatório para wait_text"}
        timeout = float(body.get("timeout", 10.0))
        interval = float(body.get("interval", 0.25))
        mon = str(body.get("monitor", "1"))
        click_on_found = bool(body.get("click", body.get("click_when_found", False)))
        offset_x = int(body.get("offset_x", 0))
        offset_y = int(body.get("offset_y", 0))
        
        start_t = time.time()
        while time.time() - start_t <= timeout:
            marks, annotated = inspect_screen_som(monitor=mon, draw_badges=False)
            for mark in marks:
                if target_text in mark.get("text", "").lower():
                    cx, cy = mark["center"]
                    click_x = cx + offset_x
                    click_y = cy + offset_y
                    res = {
                        "ok": True,
                        "action": "wait_text",
                        "found": True,
                        "text": mark.get("text"),
                        "center": [cx, cy],
                        "elapsed": round(time.time() - start_t, 3)
                    }
                    if click_on_found:
                        with input_action_lock:
                            # O resto do arquivo usa SendInput justamente porque
                            # pyautogui.click erra a coordenada com DPI escalado.
                            win32_mouse_click(click_x, click_y, button=btn)
                        res["clicked"] = True
                        res["click_coords"] = [click_x, click_y]
                    snapshot = publish_state_snapshot("som", mon, marks=marks)
                    res["frame_id"] = snapshot["frame_id"]
                    return res
            time.sleep(interval)
        return {
            "ok": False,
            "action": "wait_text",
            "found": False,
            "error": f"Texto '{target_text}' não encontrado na tela dentro de {timeout}s",
            "elapsed": round(time.time() - start_t, 3)
        }

    elif action in ("uitars", "act_uitars"):
        return execute_uitars_command(body)

    elif action in ("find_template", "match_template", "click_template", "find_and_click"):
        tpl = body.get("template") or body.get("template_path") or body.get("image") or body.get("base64")
        confidence = float(body.get("confidence", 0.8))
        mon = str(body.get("monitor", "1"))
        win = body.get("window") or body.get("focus")
        hwnd_val = body.get("hwnd")
        click = bool(body.get("click", action in ("click_template", "find_and_click")))
        res = locate_template_on_screen(tpl, confidence=confidence, monitor=mon, window=win, hwnd=hwnd_val)
        if res.get("found") and click:
            with input_action_lock:
                win32_mouse_click(res["x"], res["y"], button=btn)
            res["clicked"] = True
        return res

    elif action == "click_ui":
        name = body.get("name") or body.get("text")
        ctype = body.get("type", "ButtonControl")
        depth = int(body.get("depth", 8))
        return click_uia_element(name, control_type=ctype, max_depth=depth)

    elif action == "cursor":
        pos = pyautogui.position()
        return {"ok": True, "x": pos.x, "y": pos.y}

    elif action == "windows":
        return {"ok": True, "windows": get_open_windows()}

    elif action in ("som_exclude", "exclude_windows"):
        # O agente registra as proprias janelas (tipicamente o PID do terminal).
        # Listas vazias limpam a exclusao.
        atual = configurar_exclusao(pids=body.get("pids"), processos=body.get("processes"))
        return {"ok": True, "action": "som_exclude", "exclusion": atual}

    elif action == "hover":
        real_x, real_y, mon = coords_from_payload(body)
        duration = float(body.get("duration", body.get("wait", 0.3)))
        pyautogui.moveTo(real_x, real_y)
        time.sleep(duration)
        return {"ok": True, "action": "hover", "x": real_x, "y": real_y, "duration": duration}

    elif action == "pipeline":
        steps = body.get("steps", [])
        results = []
        for step in steps:
            res = execute_system_action(step)
            results.append(res)
            wait_s = float(step.get("wait", 0.0))
            if wait_s > 0:
                time.sleep(wait_s)
            if not res.get("ok", True):
                return {"ok": False, "error": f"Passo do pipeline falhou: {step}", "results": results}
        
        resp = {"ok": True, "action": "pipeline", "steps_executed": len(results), "results": results}
        if body.get("confirm_som", False):
            mon = str(body.get("monitor", "1"))
            marks, som_jpg = inspect_screen_som(monitor=mon, draw_badges=True)
            snapshot = publish_state_snapshot("som", mon, marks=marks, last_som_image=som_jpg)
            resp["som_marks_count"] = len(marks)
            resp["frame_id"] = snapshot["frame_id"]
            resp["image_url"] = f"/som_frame?t={int(time.time()*1000)}"
        return resp

    elif action in ("skill_compile", "compile_skill"):
        return compile_skill_from_journal(
            name=body.get("name") or body.get("skill"),
            last=body.get("last"),
            from_seq=body.get("from_seq"),
            params=body.get("params"),
            description=body.get("description"),
        )

    elif action in ("skill_run", "run_skill", "skill"):
        return run_skill(
            name=body.get("name") or body.get("skill"),
            params=body.get("params"),
            tolerance=body.get("tolerance"),
            from_step=body.get("from_step", 1),
            monitor=body.get("monitor"),
            live_state=body.get("live_state", True),
            dry_run=bool(body.get("dry_run", False)),
            reaim=body.get("reaim", True),
            reaim_confidence=body.get("reaim_confidence"),
        )

    elif action in ("skills", "skill_list"):
        return {"ok": True, "action": "skills", "skills": list_skills()}

    elif action in ("skill_forget", "forget_skill"):
        name = body.get("name") or body.get("skill")
        return {"ok": forget_skill(name), "action": "skill_forget", "skill": name}

    elif action in ("journal", "journal_start", "journal_stop", "journal_clear"):
        if action == "journal_clear" or body.get("clear"):
            removed = journal_clear()
            return {"ok": True, "action": "journal", "cleared": removed,
                    "enabled": _journal_enabled}
        if action == "journal_start":
            journal_set_enabled(True)
        elif action == "journal_stop":
            journal_set_enabled(False)
        elif "enabled" in body:
            journal_set_enabled(body["enabled"])
        entries = journal_entries(limit=body.get("limit"))
        return {"ok": True, "action": "journal", "enabled": _journal_enabled,
                "count": len(entries), "entries": entries}

    elif action in ("hover_probe", "probe"):
        if not (SOM_HOVER_PROBE or body.get("force")):
            return {"ok": False, "error_code": "hover_probe_desligado",
                    "error": "sondagem por hover mexe no mouse e vem desligada. "
                             "Ligue com SOM_HOVER_PROBE=1 ou mande force: true."}
        mon = str(body.get("monitor", "1"))
        marks = body.get("marks")
        if not marks:
            snapshot = read_state_snapshot()
            snap_marks = snapshot.get("marks") or []
            if not any(m.get("type") == "icon_candidate" for m in snap_marks):
                marks, _ = inspect_screen_som(monitor=mon, draw_badges=False,
                                              include_candidates=True)
            else:
                marks = snap_marks
        return hover_probe(marks, monitor=mon,
                           conf_min=body.get("conf_min"), conf_max=body.get("conf_max"),
                           max_probes=body.get("max_probes"),
                           settle_ms=body.get("settle_ms"),
                           budget_ms=body.get("budget_ms"))

    elif action in ("signature", "state_signature"):
        signature = compute_state_signature(
            monitor=str(body.get("monitor", "1")),
            hwnd=body.get("hwnd"),
            window_kw=body.get("window") or body.get("focus"),
            process_name=body.get("process") or body.get("process_name"),
            samples=body.get("samples", SIGNATURE_SAMPLES),
            interval=body.get("interval", SIGNATURE_SAMPLE_INTERVAL),
        )
        result = {"ok": True, "action": "signature", "signature": signature}
        expected = body.get("compare_to") or body.get("expected")
        if isinstance(expected, dict):
            result["comparison"] = signature_distance(
                expected, signature, tolerance=body.get("tolerance"))
        return result

    elif action == "state":
        state_kwargs = {
            "mode": body.get("mode", "auto"),
            "monitor": str(body.get("monitor", "1")),
            "window_kw": body.get("window"),
            "groups": body.get("groups"),
            "since": body.get("since"),
        }
        if body.get("stable", False):
            return get_stable_system_state(
                **state_kwargs,
                stable_captures=body.get("stable_captures", DEFAULT_STABLE_CAPTURES),
                interval_ms=body.get("stability_interval_ms", DEFAULT_STABLE_INTERVAL_MS),
                timeout_ms=body.get("stability_timeout_ms", DEFAULT_STABLE_TIMEOUT_MS),
            )
        return get_system_state(**state_kwargs)

    elif action == "crop":
        bbox = body.get("bbox") or body.get("roi") or body.get("rect")
        win_kw = body.get("window") or body.get("focus") or body.get("title")
        hwnd_val = body.get("hwnd")
        proc_val = body.get("process") or body.get("process_name")
        scale = float(body.get("scale", 1.0))
        quality = int(body.get("quality", 95))
        fmt = str(body.get("format", "jpeg")).lower()
        mon = str(body.get("monitor", "1"))

        crop_res = get_screen_crop(
            bbox=bbox,
            window_kw=win_kw,
            hwnd=hwnd_val,
            process_name=proc_val,
            scale=scale,
            quality=quality,
            format=fmt,
            monitor=mon
        )
        return {
            "ok": crop_res["ok"],
            "action": "crop",
            "bbox": crop_res["bbox"],
            "width": crop_res["width"],
            "height": crop_res["height"],
            "scale": crop_res["scale"],
            "format": crop_res["format"],
            "mime": crop_res["mime"],
            "image_base64": base64.b64encode(crop_res["bytes"]).decode("ascii"),
            "window": crop_res.get("window")
        }

    return {"ok": False, "error": f"Ação desconhecida: {action}"}

HTML_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>🎮 Controle Remoto Híbrido IA (UIA + SoM) - Antigravity</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0a0a0f; font-family: 'Segoe UI', sans-serif; color: #e0e0e0; user-select: none; }
  .header { background: linear-gradient(135deg, #1a1a2e, #16213e); padding: 10px 20px; border-bottom: 2px solid #00d4ff44; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 15px; color: #00d4ff; font-weight: 600; }
  .badge { background: #00d4ff22; border: 1px solid #00d4ff55; color: #00d4ff; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: bold; }
  .badge.som { background: #10b98122; border-color: #10b981; color: #10b981; }
  .live-dot { width: 8px; height: 8px; background: #00ff88; border-radius: 50%; animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  
  .mon-selector { display: flex; gap: 6px; margin-left: auto; align-items: center; }
  .mon-btn { background: #1f2937; border: 1px solid #374151; color: #9ca3af; padding: 4px 10px; border-radius: 6px; font-size: 11px; cursor: pointer; transition: all 0.15s; font-weight: 500; }
  .mon-btn:hover { background: #374151; color: #00d4ff; }
  .mon-btn.active { background: #00d4ff22; border-color: #00d4ff; color: #00d4ff; }
  .mon-btn.som-active { background: #10b98133; border-color: #10b981; color: #10b981; font-weight: bold; }
  
  .container { display: flex; height: calc(100vh - 49px); }
  .screen-area { flex: 1; position: relative; overflow: hidden; background: #050508; cursor: crosshair; display: flex; align-items: center; justify-content: center; }
  #screen { max-width: 100%; max-height: 100%; object-fit: contain; display: block; }
  .sidebar { width: 280px; background: #111827; border-left: 1px solid #1f2937; padding: 12px; overflow-y: auto; flex-shrink: 0; transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1), padding 0.25s, opacity 0.2s; }
  .sidebar.collapsed { width: 0; min-width: 0; padding: 0; border-left: none; opacity: 0; pointer-events: none; }
  
  .toggle-sidebar-btn { background: #1f2937; border: 1px solid #374151; color: #00d4ff; padding: 4px 10px; border-radius: 6px; font-size: 11px; cursor: pointer; transition: all 0.15s; font-weight: 500; display: flex; align-items: center; gap: 4px; margin-left: 6px; }
  .toggle-sidebar-btn:hover { background: #374151; color: #ffffff; border-color: #00d4ff; }
  .sidebar h3 { font-size: 10px; text-transform: uppercase; color: #6b7280; letter-spacing: 1px; margin-bottom: 8px; margin-top: 10px; }
  .sidebar h3:first-child { margin-top: 0; }
  .key-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; margin-bottom: 8px; }
  .key-btn { background: #1f2937; border: 1px solid #374151; border-radius: 6px; color: #d1d5db; font-size: 11px; padding: 7px 4px; cursor: pointer; text-align: center; transition: all 0.15s; }
  .key-btn:hover { background: #374151; border-color: #00d4ff55; color: #00d4ff; }
  .key-btn.active { background: #00d4ff22; transform: scale(0.95); }
  .key-btn.wide { grid-column: span 3; }
  
  .status-box { background: #0d1117; border: 1px solid #1f2937; border-radius: 8px; padding: 10px; margin-bottom: 12px; font-size: 11px; color: #6b7280; line-height: 1.6; }
  .status-box span { color: #00d4ff; font-weight: bold; }
  .fps-bar { height: 3px; background: #1f2937; border-radius: 2px; margin-top: 6px; overflow: hidden; }
  .fps-fill { height: 100%; background: linear-gradient(90deg, #00d4ff, #00ff88); width: 0%; transition: width 0.2s; }

  .control-group { background: #0d1117; border: 1px solid #1f2937; border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; }
  .control-group label { display: flex; justify-content: space-between; font-size: 11px; color: #9ca3af; margin-bottom: 5px; font-weight: 500; }
  .control-group label b { color: #00d4ff; }
  .control-select { width: 100%; background: #1f2937; color: #e0e0e0; border: 1px solid #374151; border-radius: 6px; padding: 5px 8px; font-size: 11px; outline: none; cursor: pointer; }
  .control-select:focus { border-color: #00d4ff; }
  .control-slider { width: 100%; -webkit-appearance: none; appearance: none; height: 5px; border-radius: 3px; background: #1f2937; outline: none; margin: 6px 0; }
  .control-slider::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 14px; height: 14px; border-radius: 50%; background: #00d4ff; cursor: pointer; box-shadow: 0 0 8px #00d4ff88; }
  .range-labels { display: flex; justify-content: space-between; font-size: 9px; color: #4b5563; }
  
  .click-btn { width: 100%; background: linear-gradient(135deg, #7c3aed, #4f46e5); border: none; border-radius: 8px; color: white; padding: 8px; cursor: pointer; font-size: 11px; margin-bottom: 6px; font-weight: 600; transition: opacity 0.2s; }
  .click-btn:hover { opacity: 0.9; }
  .click-btn.right { background: linear-gradient(135deg, #dc2626, #b91c1c); }
  .click-btn.double { background: linear-gradient(135deg, #059669, #10b981); }
  .section-divider { border: none; border-top: 1px solid #1f2937; margin: 10px 0; }
  #mouse-pos { font-size: 10px; color: #4b5563; margin-top: 4px; font-family: monospace; }
  
  .type-input-box { display: flex; gap: 4px; margin-top: 4px; }
  .type-input { flex: 1; background: #1f2937; border: 1px solid #374151; border-radius: 6px; padding: 6px; color: #fff; font-size: 11px; outline: none; }
  .type-input:focus { border-color: #00d4ff; }
  .type-btn { background: #00d4ff; border: none; border-radius: 6px; color: #000; font-weight: bold; padding: 0 10px; cursor: pointer; font-size: 11px; }

  .elem-list { max-height: 180px; overflow-y: auto; background: #080b11; border: 1px solid #1f2937; border-radius: 6px; padding: 4px; font-size: 11px; }
  .elem-item { padding: 4px 6px; border-radius: 4px; margin-bottom: 3px; background: #131b2a; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
  .elem-item:hover { background: #1f2d42; color: #00d4ff; }
  .elem-tag { background: #00d4ff33; color: #00d4ff; padding: 1px 5px; border-radius: 4px; font-weight: bold; font-size: 9px; }
</style>
</head>
<body>
<div class="header">
  <div class="live-dot"></div>
  <h1>🎮 Antigravity Remote Control</h1>
  <div class="badge" id="mode-badge">HÍBRIDO UIA + SoM</div>
  <div class="badge" id="fps-label">-- FPS</div>
  
  <div class="mon-selector">
    <button class="mon-btn active" id="btn-m1" onclick="setMonitor('1')">🖥 Tela 1</button>
    <button class="mon-btn" id="btn-m2" onclick="setMonitor('2')">🖥 Tela 2</button>
    <button class="mon-btn" id="btn-mall" onclick="setMonitor('all')">🖥 Ambas</button>
    <button class="mon-btn" id="btn-som" onclick="toggleSomMode()">🎯 Tags SoM</button>
    <button class="toggle-sidebar-btn" id="btn-toggle-sidebar" onclick="toggleSidebar()" title="Recolher / Expandir Painel">⚙️ Painel ⮞</button>
  </div>
</div>
<div class="container">
  <div class="screen-area" id="screen-area">
    <img id="screen" alt="Screen Stream">
  </div>
  <div class="sidebar" id="sidebar">
    <div class="status-box">
      <div>Status: <span id="status-text">Conectando...</span></div>
      <div>Modo: <span id="mode-text">Stream Normal</span></div>
      <div>Monitor: <span id="mon-text">Tela 1</span></div>
      <div>Frames: <span id="frame-count">0</span></div>
      <div class="fps-bar"><div class="fps-fill" id="fps-fill"></div></div>
    </div>

    <h3>🎯 Detecção Determinística (IA)</h3>
    <button class="click-btn" style="background: linear-gradient(135deg, #00d4ff, #0077ff); margin-bottom: 6px;" onclick="scanState()">🔍 Escanear Elementos & Tags</button>
    <div class="elem-list" id="elem-list">Clique acima para mapear a tela.</div>

    <hr class="section-divider">
    <h3>⚙️ Configurações de Stream</h3>
    <div class="control-group">
      <label>Resolução / Escala:</label>
      <select id="scale-select" class="control-select" onchange="updateSettings()">
        <option value="1.0" selected>100% (Nativa / HD)</option>
        <option value="0.75">75% (Equilibrado)</option>
        <option value="0.5">50% (Performance)</option>
        <option value="0.35">35% (Ultra Leve)</option>
      </select>
    </div>

    <div class="control-group">
      <label>Limitar FPS: <b id="fps-limit-val">10 FPS</b></label>
      <input type="range" id="fps-slider" class="control-slider" min="5" max="60" step="5" value="10" oninput="updateFpsSlider(this.value)" onchange="updateSettings()">
      <div class="range-labels">
        <span>5</span>
        <span>15</span>
        <span>30</span>
        <span>45</span>
        <span>60</span>
      </div>
    </div>

    <div class="control-group">
      <label>Qualidade JPEG: <b id="quality-val">90%</b></label>
      <input type="range" id="quality-slider" class="control-slider" min="20" max="95" step="5" value="90" oninput="updateQualitySlider(this.value)" onchange="updateSettings()">
    </div>
    
    <hr class="section-divider">
    <h3>Digitar Texto Rápido</h3>
    <div class="type-input-box">
      <input type="text" id="type-text" class="type-input" placeholder="Texto para colar..." onkeydown="if(event.key==='Enter') sendTypedText()">
      <button class="type-btn" onclick="sendTypedText()">Colar</button>
    </div>

    <hr class="section-divider">
    <h3>🪟 Focar Janela / Processo</h3>
    <div class="type-input-box">
      <input type="text" id="focus-target" class="type-input" placeholder="Título ou processo (ex: chrome.exe)..." onkeydown="if(event.key==='Enter') sendFocus()">
      <button class="type-btn" style="background: linear-gradient(135deg, #3b82f6, #1d4ed8); color: #fff;" onclick="sendFocus()">Focar</button>
    </div>

    <hr class="section-divider">
    <h3>⏳ Aguardar & Clicar (Wait Text)</h3>
    <div class="type-input-box" style="margin-bottom: 4px;">
      <input type="text" id="wait-target" class="type-input" placeholder="Texto a esperar na tela..." onkeydown="if(event.key==='Enter') sendWaitText()">
    </div>
    <div style="display: flex; gap: 4px; align-items: center; margin-bottom: 4px;">
      <input type="number" id="wait-ox" class="type-input" placeholder="Offset X" value="0" style="width: 50%; font-size: 10px;" title="Offset X em pixels">
      <input type="number" id="wait-oy" class="type-input" placeholder="Offset Y" value="0" style="width: 50%; font-size: 10px;" title="Offset Y em pixels">
    </div>
    <button class="click-btn" id="btn-wait-act" style="background: linear-gradient(135deg, #10b981, #059669); margin-bottom: 0;" onclick="sendWaitText()">🎯 Aguardar e Clicar</button>

    <hr class="section-divider">
    <h3>Movimento & Teclado</h3>
    <div class="key-grid">
      <div></div>
      <button class="key-btn" onmousedown="sendKey('w',true)" onmouseup="sendKey('w',false)">W</button>
      <div></div>
      <button class="key-btn" onmousedown="sendKey('a',true)" onmouseup="sendKey('a',false)">A</button>
      <button class="key-btn" onmousedown="sendKey('s',true)" onmouseup="sendKey('s',false)">S</button>
      <button class="key-btn" onmousedown="sendKey('d',true)" onmouseup="sendKey('d',false)">D</button>
      <button class="key-btn wide" onmousedown="sendKey('space',true)" onmouseup="sendKey('space',false)">ESPAÇO</button>
    </div>

    <h3>Atalhos Rápidos</h3>
    <div class="key-grid">
      <button class="key-btn" onclick="sendHotkey(['ctrl', 'c'])">CTRL+C</button>
      <button class="key-btn" onclick="sendHotkey(['ctrl', 'v'])">CTRL+V</button>
      <button class="key-btn" onclick="sendHotkey(['ctrl', 'z'])">CTRL+Z</button>
      <button class="key-btn" onclick="sendHotkey(['win', 'd'])">WIN+D</button>
      <button class="key-btn" onclick="sendHotkey(['alt', 'tab'])">ALT+TAB</button>
      <button class="key-btn" onclick="tapKey('enter')">ENTER</button>
      <button class="key-btn" onclick="tapKey('backspace')">BACK</button>
      <button class="key-btn" onclick="tapKey('tab')">TAB</button>
      <button class="key-btn" onclick="tapKey('escape')">ESC</button>
    </div>

    <hr class="section-divider">
    <h3>Ações de Mouse</h3>
    <button class="click-btn" onclick="doClick('left')">🖱 Clique Esquerdo</button>
    <button class="click-btn double" onclick="doDoubleClick()">🖱 Duplo Clique</button>
    <button class="click-btn right" onclick="doClick('right')">🖱 Clique Direito</button>
    <div style="display: flex; gap: 4px; margin-bottom: 6px;">
      <button class="key-btn" style="flex: 1; background: #3b82f622; border-color: #3b82f6; font-weight: bold; color: #60a5fa;" onclick="doMouseDown('left')">✊ Segurar</button>
      <button class="key-btn" style="flex: 1; background: #10b98122; border-color: #10b981; font-weight: bold; color: #34d399;" onclick="doMouseUp('left')">🖐 Soltar</button>
    </div>
    <div id="mouse-pos">rx: 0.00, ry: 0.00</div>
  </div>
</div>

<script>
let currentMonitor = '1';
let currentScale = 1.0;
let currentQuality = 90;
let maxFPS = 10;
let lastFrameTimestamp = 0;
let sidebarCollapsed = false;
let somMode = false;

const screenEl = document.getElementById('screen');
const screenArea = document.getElementById('screen-area');
let frameCount = 0, lastFPSTime = Date.now(), currentFPS = 0;
let lastRx = 0.5, lastRy = 0.5;
let isFetching = false;

function toggleSidebar() {
  sidebarCollapsed = !sidebarCollapsed;
  const sidebar = document.getElementById('sidebar');
  const btn = document.getElementById('btn-toggle-sidebar');
  if (sidebarCollapsed) {
    sidebar.classList.add('collapsed');
    btn.innerHTML = '⚙️ Painel ⮜';
    btn.style.color = '#9ca3af';
  } else {
    sidebar.classList.remove('collapsed');
    btn.innerHTML = '⚙️ Painel ⮞';
    btn.style.color = '#00d4ff';
  }
}

function toggleSomMode() {
  somMode = !somMode;
  const btn = document.getElementById('btn-som');
  if (somMode) {
    btn.classList.add('som-active');
    document.getElementById('mode-text').textContent = 'Tags SoM Ativas';
    scanState();
  } else {
    btn.classList.remove('som-active');
    document.getElementById('mode-text').textContent = 'Stream Normal';
  }
}

function updateFpsSlider(val) {
  document.getElementById('fps-limit-val').textContent = val >= 60 ? '60 FPS (Max)' : (val + ' FPS');
}

function updateQualitySlider(val) {
  document.getElementById('quality-val').textContent = val + '%';
}

function updateSettings() {
  currentScale = parseFloat(document.getElementById('scale-select').value);
  maxFPS = parseInt(document.getElementById('fps-slider').value, 10);
  currentQuality = parseInt(document.getElementById('quality-slider').value, 10);
}

function setMonitor(mon) {
  currentMonitor = mon;
  document.getElementById('btn-m1').className = 'mon-btn' + (mon === '1' ? ' active' : '');
  document.getElementById('btn-m2').className = 'mon-btn' + (mon === '2' ? ' active' : '');
  document.getElementById('btn-mall').className = 'mon-btn' + (mon === 'all' ? ' active' : '');
  document.getElementById('mon-text').textContent = mon === '1' ? 'Tela 1' : (mon === '2' ? 'Tela 2' : 'Ambas');
}

function scanState() {
  const listEl = document.getElementById('elem-list');
  listEl.innerHTML = '<span style="color:#9ca3af">Escaneando interface...</span>';
  fetch('/state?monitor=' + currentMonitor)
    .then(r => r.json())
    .then(data => {
      listEl.innerHTML = '';
      if (data.mode === 'uia' && data.elements && data.elements.length > 0) {
        document.getElementById('mode-text').textContent = `UIA (${data.elements.length} elementos)`;
        data.elements.forEach(el => {
          const item = document.createElement('div');
          item.className = 'elem-item';
          item.innerHTML = `<span>${el.name || el.type}</span><span class="elem-tag">ID ${el.id}</span>`;
          item.onclick = () => {
            fetch('/act', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({action: 'click_id', id: el.id}) });
          };
          listEl.appendChild(item);
        });
      } else if (data.marks && data.marks.length > 0) {
        document.getElementById('mode-text').textContent = `SoM OCR (${data.marks.length} tags)`;
        data.marks.forEach(m => {
          const item = document.createElement('div');
          item.className = 'elem-item';
          item.innerHTML = `<span>${m.text}</span><span class="elem-tag">#${m.tag}</span>`;
          item.onclick = () => {
            fetch('/act', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({action: 'click_tag', tag: m.tag}) });
          };
          listEl.appendChild(item);
        });
      } else {
        listEl.innerHTML = '<span style="color:#ef4444">Nenhum elemento encontrado.</span>';
      }
    })
    .catch(err => {
      listEl.innerHTML = `<span style="color:#ef4444">Erro: ${err.message}</span>`;
    });
}

function refreshLoop() {
  if (isFetching) return;

  const now = performance.now();
  const minInterval = 1000 / maxFPS;
  const elapsed = now - lastFrameTimestamp;

  if (elapsed < minInterval) {
    const delay = Math.max(1, minInterval - elapsed);
    setTimeout(refreshLoop, delay);
    return;
  }

  isFetching = true;
  lastFrameTimestamp = performance.now();
  
  const img = new Image();
  const endpoint = somMode ? '/som_frame' : '/frame';
  img.src = endpoint + '?monitor=' + currentMonitor + '&scale=' + currentScale + '&quality=' + currentQuality + '&t=' + Date.now();
  img.onload = () => {
    screenEl.src = img.src;
    isFetching = false;
    frameCount++;
    document.getElementById('frame-count').textContent = frameCount;
    document.getElementById('status-text').textContent = 'Online';
    
    const nowTime = Date.now();
    if (nowTime - lastFPSTime >= 1000) {
      currentFPS = Math.round((frameCount * 1000) / (nowTime - lastFPSTime));
      lastFPSTime = nowTime;
      frameCount = 0;
      document.getElementById('fps-label').textContent = currentFPS + ' FPS';
      document.getElementById('fps-fill').style.width = Math.min((currentFPS / maxFPS) * 100, 100) + '%';
    }

    const nextElapsed = performance.now() - lastFrameTimestamp;
    const waitTime = Math.max(0, minInterval - nextElapsed);
    if (waitTime > 0) {
      setTimeout(refreshLoop, waitTime);
    } else {
      requestAnimationFrame(refreshLoop);
    }
  };
  img.onerror = () => {
    isFetching = false;
    setTimeout(refreshLoop, 300);
  };
}

requestAnimationFrame(refreshLoop);

function getRelativeCoords(e) {
  const rect = screenEl.getBoundingClientRect();
  const rx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const ry = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
  return { rx, ry };
}

let isMouseDown = false;
let dragButton = 'left';
let lastDragMoveTime = 0;

screenArea.addEventListener('mousedown', (e) => {
  if (e.button !== 0 && e.button !== 2) return;
  e.preventDefault();
  isMouseDown = true;
  dragButton = e.button === 2 ? 'right' : 'left';
  const { rx, ry } = getRelativeCoords(e);
  lastRx = rx; lastRy = ry;
  screenArea.style.cursor = 'grabbing';
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'mousedown', button: dragButton, rx: rx, ry: ry, monitor: currentMonitor })
  });
});

screenArea.addEventListener('mousemove', (e) => {
  const { rx, ry } = getRelativeCoords(e);
  lastRx = rx; lastRy = ry;
  document.getElementById('mouse-pos').textContent = `rx: ${rx.toFixed(3)}, ry: ${ry.toFixed(3)}`;
  
  if (isMouseDown) {
    const now = performance.now();
    if (now - lastDragMoveTime > 35) { // ~30 updates por segundo durante arraste
      lastDragMoveTime = now;
      fetch('/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'move', rx: rx, ry: ry, monitor: currentMonitor })
      });
    }
  }
});

window.addEventListener('mouseup', (e) => {
  if (!isMouseDown) return;
  isMouseDown = false;
  screenArea.style.cursor = 'crosshair';
  const { rx, ry } = getRelativeCoords(e);
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'mouseup', button: dragButton, rx: rx, ry: ry, monitor: currentMonitor })
  });
});

screenArea.addEventListener('dblclick', (e) => {
  const { rx, ry } = getRelativeCoords(e);
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'double_click', button: 'left', rx: rx, ry: ry, monitor: currentMonitor })
  });
});

screenArea.addEventListener('contextmenu', (e) => {
  e.preventDefault();
});


screenArea.addEventListener('wheel', (e) => {
  e.preventDefault();
  const dir = e.deltaY < 0 ? 1 : -1;
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'scroll', rx: lastRx, ry: lastRy, dy: dir * 100, monitor: currentMonitor })
  });
});

function sendKey(key, down) {
  fetch('/control', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action: down?'keydown':'keyup', key:key}) });
}

function tapKey(key) {
  fetch('/control', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'key', key:key}) });
}

function sendHotkey(keys) {
  fetch('/control', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'hotkey', keys:keys}) });
}

function sendTypedText() {
  const input = document.getElementById('type-text');
  const text = input.value;
  if (!text) return;
  fetch('/control', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'type', text:text, paste: true, enter: true}) });
  input.value = '';
}

function doClick(button) {
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'click', button: button, rx: lastRx, ry: lastRy, monitor: currentMonitor })
  });
}

function doDoubleClick() {
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'double_click', rx: lastRx, ry: lastRy, monitor: currentMonitor })
  });
}

function doMouseDown(button = 'left') {
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'mousedown', button: button, rx: lastRx, ry: lastRy, monitor: currentMonitor })
  });
}

function doMouseUp(button = 'left') {
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'mouseup', button: button, rx: lastRx, ry: lastRy, monitor: currentMonitor })
  });
}

function sendFocus() {
  const input = document.getElementById('focus-target');
  const val = input.value.trim();
  if (!val) return;
  fetch('/act', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'focus', window: val })
  }).then(r => r.json()).then(res => {
    if (res.ok) {
      document.getElementById('status-text').textContent = 'Focado: ' + (res.window ? (res.window.title || res.window.process) : val);
    } else {
      alert('Janela não encontrada: ' + (res.error || val));
    }
  });
}

function sendWaitText() {
  const text = document.getElementById('wait-target').value.trim();
  const ox = parseInt(document.getElementById('wait-ox').value || 0, 10);
  const oy = parseInt(document.getElementById('wait-oy').value || 0, 10);
  if (!text) return;
  const btn = document.getElementById('btn-wait-act');
  const origText = btn.innerHTML;
  btn.innerHTML = '⏳ Aguardando...';
  btn.disabled = true;
  fetch('/wait', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'wait_text', text: text, offset_x: ox, offset_y: oy, click: true, timeout: 10, monitor: currentMonitor })
  }).then(r => r.json()).then(res => {
    btn.innerHTML = origText;
    btn.disabled = false;
    if (res.ok) {
      document.getElementById('status-text').textContent = `Clicado em '${res.text}' (${res.elapsed}s)`;
      scanState();
    } else {
      alert(res.error || 'Tempo limite esgotado.');
    }
  }).catch(err => {
    btn.innerHTML = origText;
    btn.disabled = false;
    alert('Erro: ' + err.message);
  });
}

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  fetch('/control', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'keydown', key:e.key.toLowerCase()}) });
});
document.addEventListener('keyup', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  fetch('/control', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'keyup', key:e.key.toLowerCase()}) });
});

// Se a aba perde o foco entre o keydown e o keyup, o keyup nunca chega e a tecla
// fica presa de verdade na maquina. Ao sair da aba, soltamos tudo.
function releaseAllKeys() {
  fetch('/control', { method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify({action:'release_keys'}), keepalive:true });
}
window.addEventListener('blur', releaseAllKeys);
window.addEventListener('pagehide', releaseAllKeys);
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        monitor = qs.get("monitor", ["1"])[0]

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())

        elif path in ("/frame", "/screenshot"):
            try:
                def_quality = 90
                def_scale = 1.0
                quality = int(qs.get("quality", [def_quality])[0])
                scale = float(qs.get("scale", [def_scale])[0])
                quality = max(10, min(100, quality))
                scale = max(0.1, min(2.0, scale))

                frame, _, _ = run_in_desktop_thread(capture_screen_fast, quality=quality, scale=scale, monitor=monitor, draw_cursor=True)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        elif path in ("/som_frame", "/som/image"):
            try:
                marks, annotated_jpeg = run_in_desktop_thread(inspect_screen_som, monitor=monitor, draw_badges=True)
                snapshot = publish_state_snapshot("som", monitor, marks=marks, last_som_image=annotated_jpeg)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("X-Frame-Id", snapshot["frame_id"])
                self.send_header("X-Frame-Timestamp", str(snapshot["timestamp"]))
                self.send_header("Content-Length", str(len(annotated_jpeg)))
                self.end_headers()
                self.wfile.write(annotated_jpeg)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        elif path == "/state":
            mode = qs.get("mode", ["auto"])[0]
            win_kw = qs.get("window", [None])[0]
            # ?signature=1 devolve so a assinatura barata, sem OCR nenhum.
            if qs.get("signature", ["0"])[0].lower() in ("1", "true", "yes", "on"):
                sig = run_in_desktop_thread(
                    compute_state_signature,
                    monitor=monitor,
                    window_kw=win_kw,
                    samples=int(qs.get("samples", [SIGNATURE_SAMPLES])[0]),
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "signature": sig},
                                            ensure_ascii=False).encode("utf-8"))
                return
            stable = qs.get("stable", ["0"])[0].lower() in ("1", "true", "yes", "on")
            since = qs.get("since", [None])[0]
            quer_grupos = qs.get("groups", [None])[0]
            quer_grupos = None if quer_grupos is None else (
                quer_grupos.lower() in ("1", "true", "yes", "on"))
            if stable:
                st = run_in_desktop_thread(
                    get_stable_system_state,
                    mode=mode,
                    monitor=monitor,
                    window_kw=win_kw,
                    groups=quer_grupos,
                    since=since,
                    stable_captures=qs.get("stable_captures", [DEFAULT_STABLE_CAPTURES])[0],
                    interval_ms=qs.get("stability_interval_ms", [DEFAULT_STABLE_INTERVAL_MS])[0],
                    timeout_ms=qs.get("stability_timeout_ms", [DEFAULT_STABLE_TIMEOUT_MS])[0],
                )
            else:
                st = run_in_desktop_thread(get_system_state, mode=mode, monitor=monitor,
                                           window_kw=win_kw, groups=quer_grupos,
                                           since=since)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(st, ensure_ascii=False).encode())

        elif path in ("/skills", "/skill/list"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "skills": list_skills()},
                                        ensure_ascii=False).encode("utf-8"))

        elif path == "/monitors":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(run_in_desktop_thread(get_monitors)).encode())

        elif path == "/windows":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(run_in_desktop_thread(execute_system_action, {"action": "windows"})).encode())

        elif path == "/cursor":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(run_in_desktop_thread(execute_system_action, {"action": "cursor"})).encode())

        elif path in ("/crop", "/roi"):
            try:
                raw_bbox = qs.get("bbox", [None])[0]
                win_kw = qs.get("window", [None])[0]
                hwnd_val = qs.get("hwnd", [None])[0]
                proc_val = qs.get("process", [None])[0]
                scale = float(qs.get("scale", [1.0])[0])
                quality = int(qs.get("quality", [95])[0])
                fmt = qs.get("format", ["jpeg"])[0].lower()
                as_json = qs.get("json", ["0"])[0].lower() in ("1", "true", "yes")

                res = run_in_desktop_thread(
                    get_screen_crop,
                    bbox=raw_bbox,
                    window_kw=win_kw,
                    hwnd=hwnd_val,
                    process_name=proc_val,
                    scale=scale,
                    quality=quality,
                    format=fmt,
                    monitor=monitor
                )

                if as_json:
                    payload = {
                        "ok": res["ok"],
                        "bbox": res["bbox"],
                        "width": res["width"],
                        "height": res["height"],
                        "scale": res["scale"],
                        "format": res["format"],
                        "mime": res["mime"],
                        "image_base64": base64.b64encode(res["bytes"]).decode("ascii"),
                        "window": res.get("window")
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", res["mime"])
                    self.send_header("Cache-Control", "no-cache, no-store")
                    self.send_header("Content-Length", str(len(res["bytes"])))
                    self.end_headers()
                    self.wfile.write(res["bytes"])
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        elif path in ("/stream", "/mjpeg", "/video"):
            try:
                fps = int(qs.get("fps", [20])[0])
                fps = max(1, min(60, fps))
                scale = float(qs.get("scale", [0.75])[0])
                quality = int(qs.get("quality", [75])[0])
                delay = 1.0 / fps

                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                while True:
                    t_start = time.time()
                    frame_bytes, _, _ = capture_screen_fast(quality=quality, scale=scale, monitor=monitor, draw_cursor=True)
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame_bytes)).encode() + b"\r\n\r\n"
                    )
                    self.wfile.write(header + frame_bytes + b"\r\n")
                    self.wfile.flush()
                    t_diff = time.time() - t_start
                    if delay > t_diff:
                        time.sleep(delay - t_diff)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                pass

        elif path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","version":"4.0-dev1-snapshots"}')

        elif path == "/health":
            payload = json.dumps({
                "ok": True,
                "status": "healthy",
                "version": "4.0",
                "kb_queue": _kb_queue.qsize(),
                "http_slots_free": _http_semaphore._value,
                "hwnd_cache_entries": len(_hwnd_cache),
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        # Suprime o log padrão verboso do BaseHTTPServer; usamos o structured logger.
        pass



    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 2 * 1024 * 1024:
                raise ValueError("corpo da requisição excede 2 MB")
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
            if not isinstance(body, dict):
                raise ValueError("o corpo JSON deve ser um objeto")
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return

        # Mapeamento de rotas REST dedicadas
        if path == "/click":
            body["action"] = "click"
        elif path == "/double_click":
            body["action"] = "double_click"
        elif path == "/right_click":
            body["action"] = "right_click"
        elif path == "/type":
            body["action"] = "type"
        elif path == "/key":
            body["action"] = "key"
        elif path == "/hotkey":
            body["action"] = "hotkey"
        elif path == "/drag":
            body["action"] = "drag"
        elif path == "/find":
            body["action"] = "find_and_click"
        elif path == "/focus":
            body["action"] = "focus"
        elif path == "/click_ui":
            body["action"] = "click_ui"
        elif path in ("/act", "/control"):
            pass
        elif path == "/click_id":
            body["action"] = "click_id"
        elif path == "/click_tag":
            body["action"] = "click_tag"
        elif path == "/click_text":
            body["action"] = "click_text"
        elif path == "/type_into":
            body["action"] = "type_into"
        elif path == "/click_and_verify":
            body["action"] = "click_and_verify"
        elif path in ("/skill/run", "/skills/run", "/skill"):
            body["action"] = "skill_run"
        elif path in ("/skill/compile", "/skills/compile"):
            body["action"] = "skill_compile"
        elif path in ("/skill/forget", "/skills/forget"):
            body["action"] = "skill_forget"
        elif path in ("/skills", "/skill/list"):
            body["action"] = "skills"
        elif path in ("/journal", "/journal/start", "/journal/stop", "/journal/clear"):
            body["action"] = {
                "/journal": "journal",
                "/journal/start": "journal_start",
                "/journal/stop": "journal_stop",
                "/journal/clear": "journal_clear",
            }[path]
        elif path in ("/hover_probe", "/probe"):
            body["action"] = "hover_probe"
        elif path in ("/signature", "/state_signature"):
            body["action"] = "signature"
        elif path in ("/release_keys", "/release_all"):
            body["action"] = "release_keys"
        elif path in ("/mousedown", "/mouse_down", "/down"):
            body["action"] = "mousedown"
        elif path in ("/mouseup", "/mouse_up", "/up"):
            body["action"] = "mouseup"
        elif path in ("/wait", "/wait_text", "/wait_for"):
            body["action"] = "wait_text"
        elif path == "/move":
            body["action"] = "move"
        elif path in ("/uitars", "/act_uitars"):
            body["action"] = "uitars"
        elif path in ("/find_template", "/match_template"):
            body["action"] = "find_template"
        elif path in ("/click_template", "/template"):
            body["action"] = "click_template"
        elif path in ("/crop", "/roi"):
            body["action"] = "crop"

        if path in ("/control", "/act", "/move", "/crop", "/roi", "/click", "/double_click", "/right_click", "/type", "/key", "/hotkey", "/drag", "/find", "/focus", "/click_ui", "/click_id", "/click_tag", "/click_text", "/type_into", "/click_and_verify", "/wait", "/wait_text", "/wait_for", "/mousedown", "/mouseup", "/mouse_down", "/mouse_up", "/down", "/up", "/release_keys", "/release_all", "/signature", "/state_signature", "/hover_probe", "/probe", "/journal", "/journal/start", "/journal/stop", "/journal/clear", "/skill/run", "/skills/run", "/skill", "/skill/compile", "/skills/compile", "/skill/forget", "/skills/forget", "/skills", "/skill/list", "/uitars", "/act_uitars", "/find_template", "/match_template", "/click_template", "/template"):
            acquired = _http_semaphore.acquire(timeout=5)
            if not acquired:
                log.warning("POST %s rejeitado: limite de concorrência atingido (429)", path)
                self.send_response(429)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"servidor ocupado, tente novamente"}')
                return
            t0 = time.monotonic()
            try:
                res = run_in_desktop_thread(execute_system_action, body)
                status = 409 if res.get("error_code") in (
                    "stale_frame", "expired_frame", "window_changed", "ambiguous_target",
                    "scope_unavailable", "target_not_currently_visible",
                    "repeated_failed_action", "target_occluded", "target_excluded"
                ) else 200
                if res.get("error_code") == "verification_timeout":
                    status = 408
                elif res.get("error_code") == "coordinates_out_of_bounds":
                    status = 400
            except Exception as exc:
                res = {"ok": False, "error": str(exc), "action": body.get("action")}
                status = 500
            finally:
                _http_semaphore.release()
            elapsed = (time.monotonic() - t0) * 1000
            ok_str = "ok" if res.get("ok") else "FAIL"
            log.info("POST %-22s action=%-18s %s  %.0fms", path, body.get("action", "?"), ok_str, elapsed)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def run_server(port=7842):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[OK] Servidor Híbrido rodando em http://127.0.0.1:{port}")
    print(f"[OK] Interface Web:           http://127.0.0.1:{port}")
    print(f"[OK] Inspeção de Estado (IA):  http://127.0.0.1:{port}/state")
    print(f"[OK] Frame Anotado (Tags SoM): http://127.0.0.1:{port}/som_frame")
    print(f"[OK] Screenshot Nativo:       http://127.0.0.1:{port}/screenshot")
    print(f"[OK] Stream MJPEG Contínuo:   http://127.0.0.1:{port}/stream")
    print(f"[OK] Crop ROI de Alta Def:    http://127.0.0.1:{port}/crop?bbox=x,y,w,h")
    print(f"[OK] Janelas Abertas:         http://127.0.0.1:{port}/windows")
    server.serve_forever()

def main():
    parser = argparse.ArgumentParser(description="Antigravity Remote Control CLI & Server 4.0 - Win32 SendInput + ROI Crop")
    parser.add_argument("--server", action="store_true", help="Inicia o servidor HTTP web")
    parser.add_argument("--port", type=int, default=7842, help="Porta do servidor HTTP (default: 7842)")
    
    # Comandos determinísticos de IA
    parser.add_argument("--state", action="store_true", help="Retorna o JSON do estado atual da tela (UIA ou SoM)")
    parser.add_argument("--stable", action="store_true", help="Aguarda UIA/OCR/SoM permanecerem estáveis antes de retornar o estado")
    parser.add_argument("--stable-captures", type=int, default=DEFAULT_STABLE_CAPTURES, help="Capturas consecutivas necessárias para considerar o estado estável")
    parser.add_argument("--stability-interval-ms", type=int, default=DEFAULT_STABLE_INTERVAL_MS, help="Intervalo entre capturas estáveis em milissegundos")
    parser.add_argument("--stability-timeout-ms", type=int, default=DEFAULT_STABLE_TIMEOUT_MS, help="Tempo máximo para estabilização em milissegundos")
    parser.add_argument("--som", action="store_true", help="Executa SoM via OCR nativo e imprime tags encontradas")
    parser.add_argument("--signature", action="store_true", help="Imprime a assinatura barata do estado da tela (dHash), sem OCR")
    parser.add_argument("--signature-samples", type=int, default=SIGNATURE_SAMPLES, help="Capturas usadas para marcar celulas volateis (default: 3)")
    parser.add_argument("--crop", nargs=4, type=int, metavar=("X", "Y", "W", "H"), help="Recorta região de interesse ROI da tela")
    parser.add_argument("--crop-scale", type=float, default=1.0, help="Fator de zoom/super-resolução do crop (default: 1.0)")
    parser.add_argument("--crop-out", type=str, default="crop.png", help="Arquivo de saída do crop (default: crop.png)")
    parser.add_argument("--click-tag", type=int, metavar="TAG", help="Clica na tag numérica detectada pelo SoM")
    parser.add_argument("--click-id", type=int, metavar="ID", help="Clica no elemento pelo ID UIA")
    parser.add_argument("--click-text", type=str, metavar="TEXTO", help="Busca texto na tela e clica nele")
    parser.add_argument("--wait-text", type=str, metavar="TEXTO", help="Aguarda texto específico aparecer na tela antes de prosseguir")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout em segundos para operações de wait (default: 10s)")
    parser.add_argument("--offset-x", type=int, default=0, help="Deslocamento em pixels no eixo X para o clique")
    parser.add_argument("--offset-y", type=int, default=0, help="Deslocamento em pixels no eixo Y para o clique")
    parser.add_argument("--type-into", type=str, metavar="TEXTO", help="Digita texto no elemento especificado com --click-text/tag/id")
    
    # Comandos manuais clássicos
    parser.add_argument("--move", nargs=2, type=int, metavar=("X", "Y"), help="Move o cursor para as coordenadas X Y")
    parser.add_argument("--move-rel", nargs=2, type=float, metavar=("RX", "RY"), help="Move o cursor para coordenadas relativas normalizadas RX RY")
    parser.add_argument("--smooth", action="store_true", help="Usa interpolação com aceleração e desaceleração suave (easing)")
    parser.add_argument("--duration", type=float, default=0.25, help="Duração do movimento em segundos (default: 0.25 quando suave)")
    parser.add_argument("--click", nargs=2, type=int, metavar=("X", "Y"), help="Clique absoluto nas coordenadas X Y")
    parser.add_argument("--click-rel", nargs=2, type=float, metavar=("RX", "RY"), help="Clique relativo normalizado (0.0 a 1.0)")
    parser.add_argument("--down", "--mousedown", action="store_true", help="Pressiona e mantém pressionado o botão do mouse (clicar e segurar)")
    parser.add_argument("--up", "--mouseup", action="store_true", help="Solta o botão do mouse que estava pressionado")
    parser.add_argument("--drag", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"), help="Arrasta do ponto absoluto X1 Y1 até X2 Y2")
    parser.add_argument("--drag-rel", nargs=4, type=float, metavar=("RX1", "RY1", "RX2", "RY2"), help="Arrasta do ponto relativo RX1 RY1 até RX2 RY2")
    parser.add_argument("--drag-text", nargs=2, type=str, metavar=("FROM_TEXT", "TO_TEXT"), help="Arrasta do elemento com texto de origem até o de destino")
    parser.add_argument("--drag-template", nargs=2, type=str, metavar=("FROM_TPL", "TO_TPL"), help="Arrasta do centro do template de origem até o template de destino")
    parser.add_argument("--drag-client", nargs=4, type=int, metavar=("CX1", "CY1", "CX2", "CY2"), help="Arrasta usando coordenadas da área cliente da janela")
    parser.add_argument("--drag-client-rel", nargs=4, type=float, metavar=("CRX1", "CRY1", "CRX2", "CRY2"), help="Arrasta usando coordenadas normalizadas (0.0 a 1.0) da área cliente da janela")
    parser.add_argument("--button", choices=["left", "right", "middle"], default="left", help="Botão do mouse")
    parser.add_argument("--double", action="store_true", help="Executa duplo clique")
    parser.add_argument("--monitor", default="1", help="Monitor alvo ('1', '2', 'all')")
    parser.add_argument("--window", type=str, help="Palavra-chave do título da janela para focar")
    parser.add_argument("--process", type=str, help="Nome do executável/processo da janela para focar (ex: chrome.exe)")
    parser.add_argument("--hwnd", type=int, help="Handle HWND numérico exato da janela para focar")
    parser.add_argument("--type", dest="type_text", type=str, help="Digita ou cola o texto fornecido")
    parser.add_argument("--paste", action="store_true", help="Usa colagem rápida via clipboard (recomendado)")
    parser.add_argument("--enter", action="store_true", help="Pressiona Enter após digitar")
    parser.add_argument("--press", type=str, help="Pressiona uma tecla (ex: enter, tab, esc, space)")
    parser.add_argument("--hotkey", type=str, help="Atalho de teclado (ex: 'ctrl+c', 'alt+tab')")
    parser.add_argument("--scroll", type=int, metavar="DY", help="Rola a roda do mouse")
    parser.add_argument("--x", type=int, help="Coordenada X para a ação")
    parser.add_argument("--y", type=int, help="Coordenada Y para a ação")
    parser.add_argument("--rx", type=float, help="Coordenada relativa RX")
    parser.add_argument("--ry", type=float, help="Coordenada relativa RY")
    parser.add_argument("--screenshot", nargs="?", const="screenshot.jpg", type=str, help="Salva screenshot da tela")
    parser.add_argument("--find-and-click", type=str, metavar="IMG_PATH", help="Localiza imagem de template e clica nela")
    parser.add_argument("--click-ui", type=str, metavar="NAME", help="Clica em controle de UI pelo nome (UIA)")
    parser.add_argument("--monitors", action="store_true", help="Lista monitores disponíveis")
    parser.add_argument("--windows", action="store_true", help="Lista janelas visíveis")
    parser.add_argument("--cursor", action="store_true", help="Mostra posição atual do cursor")
    parser.add_argument("--uitars", type=str, metavar="ACTION_STR", help="Executa ação no formato UI-TARS (ex: \"click(point='<point>100 200</point>')\")")
    parser.add_argument("--client-x", type=int, help="Coordenada X relativa à área cliente da janela")
    parser.add_argument("--client-y", type=int, help="Coordenada Y relativa à área cliente da janela")
    parser.add_argument("--client-rx", type=float, help="Coordenada normalizada RX (0.0 a 1.0) relativa à área cliente da janela")
    parser.add_argument("--client-ry", type=float, help="Coordenada normalizada RY (0.0 a 1.0) relativa à área cliente da janela")
    parser.add_argument("--template", type=str, metavar="TPL", help="Localiza imagem de template ou preset (ex: 'unity_play') na tela")
    parser.add_argument("--click-template", type=str, metavar="TPL", help="Localiza template ou preset e clica no centro")
    parser.add_argument("--template-conf", type=float, default=0.8, help="Confiança mínima do template matching (default: 0.8)")
    parser.add_argument("--list-templates", action="store_true", help="Lista todos os templates e presets disponíveis para clique rápido")
    parser.add_argument("--quarantine-captures", type=int, default=None, help="Número de capturas de carência para o flicker de segmentação (default: 0, opt-in)")
    parser.add_argument("--enable-dxgi-oracle", action="store_true", help="Habilita o Oráculo de Invalidação IDXGI para detecção de mudanças (opt-in)")

    args = parser.parse_args()

    if args.enable_dxgi_oracle:
        global ENABLE_DXGI_ORACLE
        ENABLE_DXGI_ORACLE = True
        get_dxgi_oracle()

    if args.quarantine_captures is not None:
        global SNAPSHOT_QUARANTINE_CAPTURES
        SNAPSHOT_QUARANTINE_CAPTURES = max(0, args.quarantine_captures)

    if args.server:
        run_server(args.port)
        return

    if args.uitars:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "uitars",
            "command": args.uitars,
            "monitor": args.monitor,
            "window": args.window,
            "process": args.process,
            "hwnd": args.hwnd
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.state:
        state_reader = get_stable_system_state if args.stable else get_system_state
        state_kwargs = {"monitor": args.monitor, "window_kw": args.window}
        if args.stable:
            state_kwargs.update({
                "stable_captures": args.stable_captures,
                "interval_ms": args.stability_interval_ms,
                "timeout_ms": args.stability_timeout_ms,
            })
        st = run_in_desktop_thread(state_reader, **state_kwargs)
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return

    if args.signature:
        sig = run_in_desktop_thread(
            compute_state_signature,
            monitor=args.monitor,
            window_kw=args.window,
            process_name=args.process,
            hwnd=args.hwnd,
            samples=args.signature_samples,
        )
        print(json.dumps(sig, ensure_ascii=False, indent=2))
        return

    if args.som:
        marks, img_bytes = run_in_desktop_thread(inspect_screen_som, monitor=args.monitor, draw_badges=True)
        with open("som_annotated.jpg", "wb") as f:
            f.write(img_bytes)
        print(f"[OK] Imagem anotada salva em som_annotated.jpg com {len(marks)} tags:")
        for m in marks:
            print(f"  #{m['tag']}: \"{m['text']}\" -> {m['center']}")
        return

    if args.click_tag:
        res = run_in_desktop_thread(execute_system_action, {"action": "click_tag", "tag": args.click_tag, "button": args.button, "offset_x": args.offset_x, "offset_y": args.offset_y})
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.click_id:
        res = run_in_desktop_thread(execute_system_action, {"action": "click_id", "id": args.click_id, "button": args.button, "offset_x": args.offset_x, "offset_y": args.offset_y})
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.wait_text:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "wait_text",
            "text": args.wait_text,
            "timeout": args.timeout,
            "click": bool(args.click_text or args.type_into),
            "offset_x": args.offset_x,
            "offset_y": args.offset_y,
            "monitor": args.monitor,
            "button": args.button
        })
        print(json.dumps(res, ensure_ascii=False))
        if args.type_into and res.get("ok"):
            time.sleep(0.05)
            t_res = run_in_desktop_thread(execute_system_action, {"action": "type", "text": args.type_into, "paste": True, "enter": args.enter})
            print(json.dumps(t_res, ensure_ascii=False))
        return

    if args.click_text:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "click_text",
            "text": args.click_text,
            "button": args.button,
            "offset_x": args.offset_x,
            "offset_y": args.offset_y,
            "monitor": args.monitor,
            "focus": args.window,
            "process": args.process,
            "hwnd": args.hwnd
        })
        print(json.dumps(res, ensure_ascii=False))
        if args.type_into and res.get("ok"):
            time.sleep(0.05)
            t_res = run_in_desktop_thread(execute_system_action, {"action": "type", "text": args.type_into, "paste": True, "enter": args.enter})
            print(json.dumps(t_res, ensure_ascii=False))
        return

    if args.monitors:
        print(json.dumps(run_in_desktop_thread(get_monitors), indent=2))
        return

    if args.windows:
        print(json.dumps(run_in_desktop_thread(execute_system_action, {"action": "windows"}), ensure_ascii=False, indent=2))
        return

    if args.cursor:
        print(json.dumps(run_in_desktop_thread(execute_system_action, {"action": "cursor"}), indent=2))
        return

    if args.screenshot:
        img_bytes, _, _ = run_in_desktop_thread(capture_screen_fast, quality=95, scale=1.0, monitor=args.monitor, draw_cursor=True)
        with open(args.screenshot, "wb") as f:
            f.write(img_bytes)
        print(f"[OK] Screenshot com cursor salva em {args.screenshot}")
        return

    if args.crop:
        crop_res = run_in_desktop_thread(
            get_screen_crop,
            bbox=[args.crop[0], args.crop[1], args.crop[2], args.crop[3]],
            window_kw=args.window,
            scale=args.crop_scale,
            format="png" if args.crop_out.lower().endswith(".png") else "jpeg",
            monitor=args.monitor
        )
        with open(args.crop_out, "wb") as f:
            f.write(crop_res["bytes"])
        print(f"[OK] Crop salvo em {args.crop_out} ({crop_res['width']}x{crop_res['height']} px, bbox: {crop_res['bbox']})")
        return

    if args.click_ui:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "click_ui",
            "name": args.click_ui,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.move:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "move",
            "x": args.move[0],
            "y": args.move[1],
            "smooth": args.smooth,
            "duration": args.duration,
            "monitor": args.monitor,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.move_rel:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "move",
            "rx": args.move_rel[0],
            "ry": args.move_rel[1],
            "smooth": args.smooth,
            "duration": args.duration,
            "monitor": args.monitor,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.click:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "double_click" if args.double else "click",
            "button": args.button,
            "x": args.click[0],
            "y": args.click[1],
            "monitor": args.monitor,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.list_templates:
        tpl_dir = os.path.join(os.path.dirname(__file__), "templates")
        tpl_list = []
        if os.path.exists(tpl_dir):
            for fn in os.listdir(tpl_dir):
                if fn.lower().endswith((".png", ".jpg", ".jpeg")):
                    name = os.path.splitext(fn)[0]
                    tpl_list.append({
                        "name": name,
                        "file": fn,
                        "example_command": f"python remote_control_server.py --click-template {name} --window <NomeDaJanela>"
                    })
        print(json.dumps({
            "ok": True,
            "templates_count": len(tpl_list),
            "directory": tpl_dir,
            "templates": tpl_list,
            "instruction_for_ai": "Para clicar sem printar a tela, execute: python remote_control_server.py --click-template <nome> --window <janela>"
        }, ensure_ascii=False, indent=2))
        return

    if args.template or args.click_template:
        tpl_name = args.click_template or args.template
        res = run_in_desktop_thread(execute_system_action, {
            "action": "click_template" if args.click_template else "find_template",
            "template": tpl_name,
            "confidence": args.template_conf,
            "monitor": args.monitor,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process,
            "button": args.button
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if (args.client_x is not None and args.client_y is not None) or (args.client_rx is not None and args.client_ry is not None):
        payload = {
            "action": "double_click" if args.double else "click",
            "button": args.button,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process
        }
        if args.client_rx is not None and args.client_ry is not None:
            payload["client_rx"] = args.client_rx
            payload["client_ry"] = args.client_ry
        else:
            payload["client_x"] = args.client_x
            payload["client_y"] = args.client_y
        res = run_in_desktop_thread(execute_system_action, payload)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.type_text:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "type",
            "text": args.type_text,
            "paste": args.paste,
            "enter": args.enter,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.press:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "key",
            "key": args.press,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.hotkey:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "hotkey",
            "keys": args.hotkey,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.down:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "mousedown",
            "button": args.button,
            "x": args.x,
            "y": args.y,
            "rx": args.rx,
            "ry": args.ry,
            "monitor": args.monitor,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.up:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "mouseup",
            "button": args.button,
            "x": args.x,
            "y": args.y,
            "rx": args.rx,
            "ry": args.ry,
            "monitor": args.monitor,
            "focus": args.window
        })
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.drag:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "drag",
            "button": args.button,
            "x1": args.drag[0],
            "y1": args.drag[1],
            "x2": args.drag[2],
            "y2": args.drag[3],
            "duration": args.duration,
            "monitor": args.monitor,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.drag_rel:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "drag",
            "button": args.button,
            "rx1": args.drag_rel[0],
            "ry1": args.drag_rel[1],
            "rx2": args.drag_rel[2],
            "ry2": args.drag_rel[3],
            "duration": args.duration,
            "monitor": args.monitor,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.drag_text:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "drag",
            "button": args.button,
            "from_text": args.drag_text[0],
            "to_text": args.drag_text[1],
            "duration": args.duration,
            "monitor": args.monitor,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.drag_template:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "drag",
            "button": args.button,
            "from_template": args.drag_template[0],
            "to_template": args.drag_template[1],
            "confidence": args.template_conf,
            "duration": args.duration,
            "monitor": args.monitor,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.drag_client:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "drag",
            "button": args.button,
            "client_x1": args.drag_client[0],
            "client_y1": args.drag_client[1],
            "client_x2": args.drag_client[2],
            "client_y2": args.drag_client[3],
            "duration": args.duration,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.drag_client_rel:
        res = run_in_desktop_thread(execute_system_action, {
            "action": "drag",
            "button": args.button,
            "client_rx1": args.drag_client_rel[0],
            "client_ry1": args.drag_client_rel[1],
            "client_rx2": args.drag_client_rel[2],
            "client_ry2": args.drag_client_rel[3],
            "duration": args.duration,
            "focus": args.window,
            "hwnd": args.hwnd,
            "process": args.process
        })
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    # Se nenhum argumento for passado, inicia o servidor
    run_server(args.port)


if __name__ == "__main__":
    main()
