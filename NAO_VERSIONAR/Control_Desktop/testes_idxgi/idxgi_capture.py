import ctypes
from ctypes import wintypes
import time
import logging

import comtypes
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT

from remote_control_server import ensure_desktop_access

logger = logging.getLogger("idxgi_capture")

# Definições Win32 / DXGI
class RECT(ctypes.Structure):
    _fields_ = [
        ('left', ctypes.c_long),
        ('top', ctypes.c_long),
        ('right', ctypes.c_long),
        ('bottom', ctypes.c_long)
    ]

    def width(self):
        return max(0, self.right - self.left)

    def height(self):
        return max(0, self.bottom - self.top)

    def area(self):
        return self.width() * self.height()

    def as_list(self):
        return [self.left, self.top, self.right, self.bottom]

    def intersect(self, other):
        nl = max(self.left, other.left)
        nt = max(self.top, other.top)
        nr = min(self.right, other.right)
        nb = min(self.bottom, other.bottom)
        if nr > nl and nb > nt:
            return RECT(nl, nt, nr, nb)
        return None

class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

class DXGI_OUTDUPL_MOVE_RECT(ctypes.Structure):
    _fields_ = [
        ('SourcePoint', POINT),
        ('DestinationRect', RECT)
    ]

class DXGI_OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [
        ('Position', POINT),
        ('Visible', wintypes.BOOL)
    ]

from dxcam._libs.dxgi import DXGI_OUTDUPL_FRAME_INFO

# Constantes DXGI
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_INVALID_CALL = 0x887A0001
E_ACCESSDENIED = 0x80070005

# Protótipos de vtable para IDXGIOutputDuplication
GET_DIRTY_RECTS_PROTO = ctypes.WINFUNCTYPE(
    wintypes.HRESULT,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(RECT),
    ctypes.POINTER(wintypes.UINT)
)

GET_MOVE_RECTS_PROTO = ctypes.WINFUNCTYPE(
    wintypes.HRESULT,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(DXGI_OUTDUPL_MOVE_RECT),
    ctypes.POINTER(wintypes.UINT)
)

# Nota arquitetural sobre DuplicateOutput:
# A API IDXGIOutput1::DuplicateOutput suporta até 4 conexões/sessões simultâneas por monitor
# no sistema (gerenciadas pelo DWM/Windows). No entanto, um mesmo processo/dispositivo Direct3D
# não deve duplicar a mesma saída múltiplas vezes sem conflito. A adoção de Singleton dentro
# da nossa arquitetura visa a centralização do ciclo de vida do D3D11 e o gerenciamento limpo
# de estado e recuperação, e não uma suposta exclusividade global do monitor.

class DXGIRecoveryError(Exception):
    pass

class DXGIOutputDuplicator:
    def __init__(self, device_idx=0, output_idx=0):
        ensure_desktop_access()
        self.device_idx = device_idx
        self.output_idx = output_idx
        self.cam = None
        self.dupl = None
        self._get_dirty_fn = None
        self._get_move_fn = None
        self.screen_width = 1920
        self.screen_height = 1080
        self.frame_held = False
        self._inicializar()

    def _inicializar(self):
        import dxcam
        ensure_desktop_access()
        if self.cam is not None:
            try:
                self.release()
            except Exception:
                pass
                
        try:
            self.cam = dxcam.create(device_idx=self.device_idx, output_idx=self.output_idx)
            self.dupl = self.cam._duplicator.duplicator
            self.screen_width = self.cam.width
            self.screen_height = self.cam.height
            
            # Extrair vtable
            vptr = ctypes.cast(self.dupl, ctypes.POINTER(ctypes.c_void_p))[0]
            vtable = ctypes.cast(vptr, ctypes.POINTER(ctypes.c_void_p))
            
            # Index 9: GetFrameDirtyRects, Index 10: GetFrameMoveRects
            self._get_dirty_fn = GET_DIRTY_RECTS_PROTO(vtable[9])
            self._get_move_fn = GET_MOVE_RECTS_PROTO(vtable[10])
            self.frame_held = False
            logger.info("DXGIOutputDuplication inicializado com sucesso (%dx%d)", self.screen_width, self.screen_height)
        except Exception as e:
            logger.error("Falha ao inicializar DXGI: %s", e)
            raise

    def acquire_frame(self, timeout_ms=50, capture_pixels=False):
        """
        Captura o próximo frame e metadados de dirty/move rects.
        Retorna dicionário com estatísticas completas e, opcionalmente, o buffer de pixels.
        """
        ensure_desktop_access()
        if self.dupl is None:
            logger.warning("Duplicator nulo detectado. Reinicializando pipeline...")
            try:
                self._inicializar()
                return {
                    "status": "recovered_after_access_lost",
                    "updated": False,
                    "is_timeout": False,
                    "acquire_latency_ms": None,
                    "error": "0x887A0026"
                }
            except Exception as e:
                return {
                    "status": "error_access_lost",
                    "updated": False,
                    "is_timeout": False,
                    "acquire_latency_ms": None,
                    "rec_error": str(e)
                }

        if self.frame_held:
            try:
                self.dupl.ReleaseFrame()
            except Exception:
                pass
            self.frame_held = False

        from dxcam._libs.dxgi import IDXGIResource
        from dxcam._libs.d3d11 import ID3D11Texture2D
        info = DXGI_OUTDUPL_FRAME_INFO()
        res = ctypes.POINTER(IDXGIResource)()
        
        t0 = time.perf_counter()
        try:
            hr = self.dupl.AcquireNextFrame(timeout_ms, ctypes.byref(info), ctypes.byref(res))
        except comtypes.COMError as ce:
            hr = ce.hresult
            
        t_acquire_end = time.perf_counter()
        acquire_time_ms = (t_acquire_end - t0) * 1000.0
        u_hr = hr & 0xFFFFFFFF
        
        # 1. Caso de Timeout (nenhuma mudança de frame ocorrida no tempo estipulado)
        if u_hr == DXGI_ERROR_WAIT_TIMEOUT:
            return {
                "status": "timeout",
                "updated": False,
                "is_timeout": True,
                "timeout_wait_ms": acquire_time_ms,
                "acquire_latency_ms": None,
                "accumulated_frames": 0,
                "dirty_rects": [],
                "dirty_area": 0,
                "dirty_pct_screen": 0.0,
                "move_rects": [],
                "union_rects": [],
                "union_area": 0,
                "union_pct_screen": 0.0,
                "is_full_frame": False,
                "pointer_updated": False,
                "pointer_visible": False,
                "pointer_pos": None,
                "pixels": None
            }
            
        # 2. Caso de perda de acesso (AccessLost, DeviceRemoved, ModeChange)
        if u_hr in (DXGI_ERROR_ACCESS_LOST, E_ACCESSDENIED, DXGI_ERROR_INVALID_CALL):
            logger.warning("DXGI Erro de transição/perda: 0x%08X. Tentando reinicializar...", u_hr)
            time.sleep(0.1)
            try:
                self._inicializar()
                return {
                    "status": "recovered_after_access_lost",
                    "updated": False,
                    "is_timeout": False,
                    "acquire_latency_ms": None,
                    "error": hex(u_hr)
                }
            except Exception as rec_err:
                return {
                    "status": "error_access_lost",
                    "updated": False,
                    "is_timeout": False,
                    "acquire_latency_ms": None,
                    "error": hex(u_hr),
                    "rec_error": str(rec_err)
                }

        if u_hr != 0:
            return {
                "status": "error",
                "hresult": hex(u_hr),
                "updated": False,
                "is_timeout": False,
                "acquire_latency_ms": None
            }

        # Frame adquirido com sucesso
        self.frame_held = True
        
        # Obter dirty rects
        dirty_rects = []
        dirty_area = 0
        buf_size = max(4096, info.TotalMetadataBufferSize)
        n_max = buf_size // ctypes.sizeof(RECT)
        dirty_buf = (RECT * n_max)()
        req_dirty = wintypes.UINT(0)
        
        hr_dirty = self._get_dirty_fn(self.dupl, buf_size, dirty_buf, ctypes.byref(req_dirty))
        if (hr_dirty & 0xFFFFFFFF) == 0:
            count = req_dirty.value // ctypes.sizeof(RECT)
            for i in range(count):
                r = dirty_buf[i]
                dirty_rects.append(r.as_list())
                dirty_area += r.area()
                
        # Obter move rects
        move_rects = []
        n_move_max = buf_size // ctypes.sizeof(DXGI_OUTDUPL_MOVE_RECT)
        move_buf = (DXGI_OUTDUPL_MOVE_RECT * n_move_max)()
        req_move = wintypes.UINT(0)
        
        hr_move = self._get_move_fn(self.dupl, buf_size, move_buf, ctypes.byref(req_move))
        if (hr_move & 0xFFFFFFFF) == 0:
            m_count = req_move.value // ctypes.sizeof(DXGI_OUTDUPL_MOVE_RECT)
            for i in range(m_count):
                mr = move_buf[i]
                move_rects.append({
                    "source": [mr.SourcePoint.x, mr.SourcePoint.y],
                    "destination": mr.DestinationRect.as_list()
                })

        # Construir União Completa das Regiões Alteradas (Dirty + Move Dst + Move Src)
        union_rects = []
        for dr in dirty_rects:
            union_rects.append(dr)
        for mr in move_rects:
            dx1, dy1, dx2, dy2 = mr["destination"]
            union_rects.append([dx1, dy1, dx2, dy2])
            sx, sy = mr["source"]
            mw = dx2 - dx1
            mh = dy2 - dy1
            union_rects.append([sx, sy, sx + mw, sy + mh])

        # Calcular área da união (com união geométrica simplificada de bounding boxes)
        screen_area = self.screen_width * self.screen_height
        pct_screen = (dirty_area / screen_area) * 100.0 if screen_area > 0 else 0.0
        is_full = (pct_screen >= 99.5) or (len(dirty_rects) == 1 and dirty_rects[0] == [0, 0, self.screen_width, self.screen_height])

        # Metadados de ponteiro
        ptr_pos = info.PointerPosition
        pointer_updated = (info.LastMouseUpdateTime > 0)

        # Captura de pixels se requisitado
        pixels = None
        if capture_pixels and res:
            try:
                self.cam._duplicator.texture = res.QueryInterface(ID3D11Texture2D)
                cw, ch = self.cam._copy_region_to_stage(self.cam.region)
                raw_frame = self.cam._process_staging_frame(cw, ch)
                if raw_frame is not None:
                    pixels = raw_frame.copy()
            except Exception as pe:
                logger.debug("Falha na extração de pixels: %s", pe)

        # Liberar frame
        try:
            self.dupl.ReleaseFrame()
        except Exception:
            pass
        self.frame_held = False

        return {
            "status": "ok",
            "updated": True,
            "is_timeout": False,
            "acquire_latency_ms": acquire_time_ms,
            "accumulated_frames": info.AccumulatedFrames,
            "coalesced": bool(info.RectsCoalesced),
            "dirty_rects": dirty_rects,
            "dirty_count": len(dirty_rects),
            "dirty_area": dirty_area,
            "dirty_pct_screen": round(pct_screen, 2),
            "is_full_frame": is_full,
            "move_rects": move_rects,
            "move_count": len(move_rects),
            "union_rects": union_rects,
            "union_count": len(union_rects),
            "pointer_updated": pointer_updated,
            "pointer_visible": bool(ptr_pos.Visible),
            "pointer_pos": [ptr_pos.Position.x, ptr_pos.Position.y] if ptr_pos.Visible else None,
            "pixels": pixels
        }

    def force_access_lost_simulation(self):
        """Simula a perda do duplicator liberando a interface COM."""
        if self.dupl:
            try:
                self.dupl.Release()
            except Exception:
                pass
            self.dupl = None

    def release(self):
        if self.frame_held and self.dupl:
            try:
                self.dupl.ReleaseFrame()
            except Exception:
                pass
            self.frame_held = False
        if self.cam:
            try:
                self.cam.release()
            except Exception:
                pass
            self.cam = None
            self.dupl = None
