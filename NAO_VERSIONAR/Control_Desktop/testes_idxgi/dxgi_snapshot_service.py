import os
import sys
import time
import logging
import threading
import ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    release_desktop_access,
    user32
)
from testes_idxgi.idxgi_capture import DXGIOutputDuplicator, RECT

logger = logging.getLogger("dxgi_snapshot_service")

class DXGISnapshotService:
    """
    Singleton que opera como Oráculo de Invalidação (Invalidation Oracle) via IDXGIOutputDuplication.
    
    Diretrizes Arquiteturais:
    1. Trata IDXGI como oráculo de 'mudou / não mudou', nunca substituindo o pipeline síncrono.
    2. Suporta Watermark / Geração pós-ação para eliminar race conditions de renderização.
    3. Trata casos de borda: múltiplos monitores, janelas fora da tela, DPIs mistos,
       mudança de modo/resolução, frames acumulados e frames puramente de cursor.
    4. Em qualquer incerteza, erro ou caso não coberto, aciona fallback para Full Snapshot.
    """
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, device_idx=0, output_idx=0, enabled=True):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(device_idx=device_idx, output_idx=output_idx, enabled=enabled)
            return cls._instance

    def __init__(self, device_idx=0, output_idx=0, enabled=True):
        self.device_idx = device_idx
        self.output_idx = output_idx
        self.enabled = enabled
        
        self.running = False
        self.worker_thread = None
        
        # Estado do Oráculo
        self.state_lock = threading.Lock()
        self.generation = 1
        self.last_present_time = 0
        self.last_mouse_time = 0
        self.last_pointer_pos = None
        self.screen_width = 1920
        self.screen_height = 1080
        
        # Histórico de alterações por geração (anel com max 128 entradas)
        self.history_limit = 128
        self.history = {} # generation -> list of [l, t, r, b]
        
        # Monitores conhecidos
        self.monitors = []
        self.is_multimonitor = False
        
        # Invalidação forçada
        self.last_invalidation_reason = None
        self.last_mode_change_time = 0
        self.paused = False

    def start(self):
        if not self.enabled:
            logger.info("DXGISnapshotService desativado por configuração (opt-in).")
            return False
            
        with self.state_lock:
            if self.running:
                return True
            self.running = True
            
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="DXGISnapshotWorker")
        self.worker_thread.start()
        logger.info("DXGISnapshotService iniciado em background thread.")
        return True

    def stop(self):
        with self.state_lock:
            self.running = False
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=1.0)
        logger.info("DXGISnapshotService finalizado.")

    def _atualizar_monitores(self):
        """Mapeia os monitores do sistema para detectar janelas que atravessam monitores."""
        try:
            import win32api
            mons = []
            for m in win32api.EnumDisplayMonitors():
                r = m[2]
                mons.append([r[0], r[1], r[2], r[3]])
            self.monitors = mons
            self.is_multimonitor = (len(mons) > 1)
        except Exception as e:
            logger.debug("Falha ao enumerar monitores: %s", e)
            self.monitors = [[0, 0, self.screen_width, self.screen_height]]
            self.is_multimonitor = False

    def _worker_loop(self):
        ensure_desktop_access()
        duplicator = None
        try:
            self._atualizar_monitores()
            duplicator = DXGIOutputDuplicator(device_idx=self.device_idx, output_idx=self.output_idx)
            self.screen_width = duplicator.screen_width
            self.screen_height = duplicator.screen_height
            
            while True:
                with self.state_lock:
                    if not self.running:
                        break
                    if self.paused:
                        time.sleep(0.02)
                        continue
                        
                # Adquirir próximo frame com timeout de 25ms (loop de evento responsivo)
                res = duplicator.acquire_frame(timeout_ms=25, capture_pixels=False)
                status = res.get("status")
                
                # 1. Recuperação após perda de acesso / troca de resolução
                if status in ("recovered_after_access_lost", "error_access_lost"):
                    with self.state_lock:
                        self.generation += 1
                        self.last_mode_change_time = time.time()
                        self.last_invalidation_reason = f"dxgi_transition_{status}"
                        self.history.clear()
                        self._atualizar_monitores()
                    time.sleep(0.05)
                    continue
                    
                # 2. Timeout (nenhuma mudança na tela)
                if res.get("is_timeout"):
                    continue
                    
                # 3. Frame com atualização
                if status == "ok":
                    with self.state_lock:
                        # Identificar se foi atualização pura de cursor de mouse
                        dirty_count = res.get("dirty_count", 0)
                        move_count = res.get("move_count", 0)
                        ptr_updated = res.get("pointer_updated", False)
                        
                        if dirty_count == 0 and move_count == 0 and ptr_updated:
                            # Apenas o ponteiro mexeu: NÃO incrementa geração de pixels
                            self.last_mouse_time = time.time()
                            self.last_pointer_pos = res.get("pointer_pos")
                            continue
                            
                        # Houve mudança de conteúdo real (dirty rects ou move rects)
                        # Incremento monotônico: um acquire representa um evento observado
                        self.generation += 1
                        self.last_present_time = time.time()
                        
                        union_rects = res.get("union_rects", [])
                        accum = res.get("accumulated_frames", 1) or 1
                        self.history[self.generation] = {
                            "union_rects": union_rects,
                            "timestamp": time.time(),
                            "accumulated_frames": accum
                        }
                        
                        # Limitar histórico na memória
                        if len(self.history) > self.history_limit:
                            min_gen = min(self.history.keys())
                            del self.history[min_gen]
                            
                time.sleep(0.005)
                
        except Exception as e:
            logger.error("Erro fatal no loop de DXGISnapshotService: %s", e)
            with self.state_lock:
                self.last_invalidation_reason = f"worker_exception_{type(e).__name__}"
        finally:
            if duplicator:
                duplicator.release()
            release_desktop_access()

    def get_watermark(self):
        """Retorna a geração/watermark atual antes de disparar uma ação."""
        with self.state_lock:
            return self.generation

    def _rects_intersect_window(self, rects, win_rect):
        """Verifica se algum retângulo da lista intersecta a janela-alvo visível."""
        if not win_rect:
            return len(rects) > 0, len(rects), 0
        wl, wt, wr, wb = win_rect
        vw_left = max(0, min(self.screen_width, wl))
        vw_top = max(0, min(self.screen_height, wt))
        vw_right = max(0, min(self.screen_width, wr))
        vw_bottom = max(0, min(self.screen_height, wb))
        if vw_right <= vw_left or vw_bottom <= vw_top:
            return False, 0, 0
            
        hits = 0
        dirty_area = 0
        for r in rects:
            il = max(vw_left, r[0])
            it = max(vw_top, r[1])
            ir = min(vw_right, r[2])
            ib = min(vw_bottom, r[3])
            if ir > il and ib > it:
                hits += 1
                dirty_area += (ir - il) * (ib - it)
        return hits > 0, hits, dirty_area

    def wait_for_settle(self, watermark, win_rect=None, quiescence_window_ms=40, min_observe_ms=30, max_timeout_ms=300, expected_delay_ms=0, timeout_ms=None, min_wait_ms=None):
        """
        Estratégia de Quiescência pós-ação:
        Elimina tanto falso cache hit quanto snapshot prematuro.

        Mecanismo:
        1. Watermark inicial: observa gerações posteriores ao envio da ação.
        2. Primeiro dirty na janela marca 'target_activity_seen' e inicia/reinicia o timer.
        3. Novas alterações na janela continuam reiniciando o timer de silêncio.
        4. O estado só é considerado 'settled_quiescent' quando a janela permanecer sem alterações
           por pelo menos 'quiescence_window_ms' (ex: 40-50ms) APÓS o término da atividade.
        5. Timeout Máximo Absoluto ('max_timeout_ms'): se a janela nunca parar de renderizar (ex: Unity
           a 60 FPS ou caret), encerra com 'target_continuous_activity_timeout' (never_quiescent=True).
        6. Ausência de atividade: se nenhuma alteração for vista até o timeout, postura conservadora
           força Full Snapshot com 'action_target_no_activity_timeout'.
        """
        if timeout_ms is not None:
            max_timeout_ms = timeout_ms
        if min_wait_ms is not None:
            min_observe_ms = min_wait_ms
        if not self.enabled:
            return True, "dxgi_disabled", {"fallback": True, "quiescent": False}
            
        t0 = time.time()
        max_timeout_s = max_timeout_ms / 1000.0
        min_observe_s = min_observe_ms / 1000.0
        expected_delay_s = expected_delay_ms / 1000.0
        
        # Espera mínima de trânsito de input
        time.sleep(min_observe_s)
        
        target_activity_seen = False
        first_target_time = None
        last_target_time = None
        target_events_count = 0
        target_dirty_area = 0
        
        other_windows_active = False
        other_rects_count = 0
        
        settled_quiescent = False
        last_eval_gen = watermark
        
        while (time.time() - t0) < max_timeout_s:
            with self.state_lock:
                curr_gen = self.generation
                gens_to_check = [g for g in range(last_eval_gen + 1, curr_gen + 1) if g in self.history]
                
            for g in gens_to_check:
                entry = self.history.get(g, {})
                rects = entry.get("union_rects", [])
                if not rects:
                    continue
                    
                if win_rect:
                    hit, count, area = self._rects_intersect_window(rects, win_rect)
                    if hit:
                        now_hit = time.time()
                        target_activity_seen = True
                        target_events_count += 1
                        target_dirty_area += area
                        last_target_time = now_hit
                        if first_target_time is None:
                            first_target_time = now_hit
                    else:
                        other_windows_active = True
                        other_rects_count += len(rects)
                else:
                    now_hit = time.time()
                    target_activity_seen = True
                    target_events_count += 1
                    target_dirty_area += len(rects)
                    last_target_time = now_hit
                    if first_target_time is None:
                        first_target_time = now_hit
                        
            last_eval_gen = curr_gen
            now = time.time()
            
            # Checagem de Quiescência Estável:
            if target_activity_seen and last_target_time is not None:
                silence_ms = (now - last_target_time) * 1000.0
                elapsed_ms = (now - t0) * 1000.0
                if silence_ms >= quiescence_window_ms and elapsed_ms >= expected_delay_ms:
                    settled_quiescent = True
                    break
                    
            time.sleep(0.008)
            
        now_end = time.time()
        total_elapsed_ms = round((now_end - t0) * 1000.0, 2)
        
        # Desfecho 1: Quiescência Estabilizada com Sucesso
        if settled_quiescent:
            with self.state_lock:
                self.last_invalidation_reason = None
            return True, "target_settled_quiescent", {
                "quiescent": True,
                "temporally_quiescent": True,
                "outcome_verified": False,
                "never_quiescent": False,
                "target_activity_seen": True,
                "elapsed_ms": total_elapsed_ms,
                "first_activity_ms": round((first_target_time - t0) * 1000.0, 2) if first_target_time else 0.0,
                "last_activity_ms": round((last_target_time - t0) * 1000.0, 2) if last_target_time else 0.0,
                "silence_ms": round((now_end - last_target_time) * 1000.0, 2) if last_target_time else 0.0,
                "activity_events": target_events_count,
                "dirty_area": target_dirty_area
            }
            
        # Desfecho 2: Atividade Vista, mas NUNCA estabilizou dentro do timeout (renderização contínua / loop)
        if target_activity_seen:
            with self.state_lock:
                self.last_invalidation_reason = "target_continuous_activity"
            return True, "target_continuous_activity_timeout", {
                "quiescent": False,
                "never_quiescent": True,
                "target_activity_seen": True,
                "elapsed_ms": total_elapsed_ms,
                "activity_events": target_events_count,
                "fallback": True
            }
            
        # Desfecho 3: Nenhuma atividade detectada no alvo (ação inerte ou alvo muito lento)
        with self.state_lock:
            motivo = "action_target_no_activity_timeout" if not other_windows_active else "action_target_unsettled_other_windows_active"
            self.last_invalidation_reason = motivo
            
        return True, motivo, {
            "quiescent": False,
            "never_quiescent": False,
            "target_activity_seen": False,
            "fallback": True,
            "other_windows_active": other_windows_active,
            "elapsed_ms": total_elapsed_ms
        }

    def has_changed_since(self, watermark, win_rect=None):
        """
        Oráculo de Invalidação: responde se houve alteração na região informada desde o watermark.
        """
        if not self.enabled:
            return True, "dxgi_disabled", {"fallback": True}
            
        with self.state_lock:
            # Se o worker não estiver rodando ou sofreu exceção
            if not self.running:
                return True, "worker_not_running", {"fallback": True}
                
            # Se houve invalidação pendente (ex: ação não-settled, transição de tela, access lost)
            if self.last_invalidation_reason:
                reason = self.last_invalidation_reason
                self.last_invalidation_reason = None
                return True, f"invalidated_due_to_{reason}", {"fallback": True}
                
            # Se o watermark for muito antigo e já saiu do histórico
            if watermark < (self.generation - self.history_limit):
                return True, "watermark_expired_from_history", {"fallback": True}

            # Avaliação prévia de Casos de Borda Geométricos da Janela-Alvo
            if win_rect:
                wl, wt, wr, wb = win_rect
                
                # Caso de Borda A: Janela atravessando múltiplos monitores
                if self.is_multimonitor:
                    mon_intersections = 0
                    for ml, mt, mr, mb in self.monitors:
                        if max(wl, ml) < min(wr, mr) and max(wt, mt) < min(wb, mb):
                            mon_intersections += 1
                    if mon_intersections > 1:
                        return True, "window_crosses_multiple_monitors", {"fallback": True}
                        
                # Caso de Borda B: Janela fora dos limites do monitor duplicado
                if wr <= 0 or wb <= 0 or wl >= self.screen_width or wt >= self.screen_height:
                    return True, "window_completely_offscreen", {"fallback": True}
                    
                # Caso de Borda C: Janela parcialmente fora da tela (menos de 20% visível)
                vw_left = max(0, min(self.screen_width, wl))
                vw_top = max(0, min(self.screen_height, wt))
                vw_right = max(0, min(self.screen_width, wr))
                vw_bottom = max(0, min(self.screen_height, wb))
                
                visible_area = max(0, vw_right - vw_left) * max(0, vw_bottom - vw_top)
                total_win_area = max(1, (wr - wl) * (wb - wt))
                if visible_area / total_win_area < 0.2:
                    return True, "window_mostly_offscreen", {"fallback": True}

            # Se nenhuma geração nova foi emitida desde o watermark
            if self.generation <= watermark:
                return False, "no_frames_presented", {
                    "watermark": watermark,
                    "current_generation": self.generation,
                    "dirty_rects_count": 0
                }
                
            # Coleta todas as regiões alteradas entre watermark + 1 e generation atual
            accumulated_rects = []
            for gen in range(watermark + 1, self.generation + 1):
                if gen in self.history:
                    accumulated_rects.extend(self.history[gen].get("union_rects", []))
                    
            if not accumulated_rects:
                return False, "no_pixel_changes", {
                    "watermark": watermark,
                    "current_generation": self.generation,
                    "dirty_rects_count": 0
                }

        # Avaliação de Interseção Espacial com a Janela-Alvo
        if win_rect:
            hit, count, dirty_win_area = self._rects_intersect_window(accumulated_rects, win_rect)
            if not hit:
                # Mudanças ocorreram em OUTRA janela, mas NÃO dentro da janela-alvo!
                # É SEGURO reutilizar o cache da janela-alvo durante observação ociosa.
                return False, "changes_outside_target_window", {
                    "watermark": watermark,
                    "current_generation": self.generation,
                    "target_dirty_area": 0,
                    "target_dirty_pct": 0.0
                }
                
            wl, wt, wr, wb = win_rect
            vw_left = max(0, min(self.screen_width, wl))
            vw_top = max(0, min(self.screen_height, wt))
            vw_right = max(0, min(self.screen_width, wr))
            vw_bottom = max(0, min(self.screen_height, wb))
            visible_area = max(1, (vw_right - vw_left) * (vw_bottom - vw_top))
            dirty_pct = round((dirty_win_area / visible_area) * 100.0, 2)
            
            return True, "target_window_dirty", {
                "watermark": watermark,
                "current_generation": self.generation,
                "target_dirty_area": dirty_win_area,
                "target_dirty_pct": dirty_pct,
                "intersecting_rects_count": count
            }
            
        return True, "screen_dirty", {
            "watermark": watermark,
            "current_generation": self.generation,
            "accumulated_rects_count": len(accumulated_rects)
        }
