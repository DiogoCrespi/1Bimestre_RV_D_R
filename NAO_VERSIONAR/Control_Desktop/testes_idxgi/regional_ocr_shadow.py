import os
import sys
import time
import math
import logging
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from remote_control_server import (
    ensure_desktop_access,
    _ocr_candidates
)
import remote_control_server

logger = logging.getLogger("regional_ocr_shadow")

def merge_rectangles(rects, margin=15, merge_distance=25):
    """
    Expande retângulos com margem defensiva e funde retângulos que se intersectam
    ou estão a uma distância menor que merge_distance.
    """
    if not rects:
        return []
        
    expanded = []
    for (l, t, r, b) in rects:
        expanded.append([l - margin, t - margin, r + margin, b + margin])
        
    merged = True
    while merged:
        merged = False
        new_list = []
        visited = [False] * len(expanded)
        
        for i in range(len(expanded)):
            if visited[i]:
                continue
            cur = expanded[i]
            for j in range(i + 1, len(expanded)):
                if visited[j]:
                    continue
                other = expanded[j]
                if (max(cur[0], other[0]) <= min(cur[2], other[2]) + merge_distance and
                    max(cur[1], other[1]) <= min(cur[3], other[3]) + merge_distance):
                    cur = [
                        min(cur[0], other[0]),
                        min(cur[1], other[1]),
                        max(cur[2], other[2]),
                        max(cur[3], other[3])
                    ]
                    visited[j] = True
                    merged = True
            new_list.append(cur)
            visited[i] = True
            
        expanded = new_list
        
    return expanded

def compute_adaptive_margin(existing_marks, default_margin=20):
    """
    Calcula margem adaptativa baseada na altura média das linhas de texto (ex: 1.5x a altura média).
    """
    if not existing_marks:
        return default_margin
    heights = []
    for m in existing_marks:
        b = m.get("box")
        if b and (b[3] - b[1]) > 5:
            heights.append(b[3] - b[1])
    if not heights:
        return default_margin
    avg_h = sum(heights) / len(heights)
    return max(15, min(40, int(avg_h * 1.5)))

def expand_for_reflow_containers(regions, win_rect=None):
    """
    Regra para containers de reflow (Inspector e Árvores/Listas como Hierarchy na Unity):
    Se a alteração estiver em colunas de layout vertical (Hierarchy à esquerda ou Inspector à direita),
    qualquer expansão/recolhimento de nó ou componente desloca verticalmente todos os
    elementos abaixo.
    Portanto, a região é expandida verticalmente até a base da janela para englobar todo o reflow.
    """
    if not regions or not win_rect:
        return regions
        
    wl, wt, wr, wb = win_rect
    win_w = wr - wl
    hierarchy_threshold_x = wl + int(win_w * 0.25)
    inspector_threshold_x = wl + int(win_w * 0.68)
    console_top_y = wt + int((wb - wt) * 0.65)
    console_right_x = wl + int(win_w * 0.50)
    
    expanded_regions = []
    for (rl, rt, rr, rb) in regions:
        # 1. Se intersecta a metade inferior da janela (Console / Project / Logs)
        if rb > console_top_y and (rt >= console_top_y - 80 or rb - console_top_y > 100):
            expanded_regions.append([wl, console_top_y, max(rr, console_right_x), wb])
        # 2. Se intersecta a coluna do Inspector (direita)
        elif rr > inspector_threshold_x:
            expanded_regions.append([max(wl, min(rl, inspector_threshold_x - 10)), rt, wr, wb])
        # 3. Se intersecta a coluna da Hierarchy / árvore (esquerda)
        elif rl < hierarchy_threshold_x:
            expanded_regions.append([wl, rt, min(wr, hierarchy_threshold_x + 10), wb])
        else:
            expanded_regions.append([rl, rt, rr, rb])
            
    return merge_rectangles(expanded_regions, margin=0, merge_distance=10)

def expand_with_intersecting_marks(regions, existing_marks, win_rect=None):
    """
    Expande as regiões para cobrir integralmente qualquer elemento SoM existente
    cuja bounding box intersecte a região alterada. Isso impede fatiamento de tags ao meio.
    """
    if not existing_marks or not regions:
        return regions
        
    result_regions = [list(r) for r in regions]
    
    for r in result_regions:
        rl, rt, rr, rb = r
        for m in existing_marks:
            box = m.get("box") or m.get("rect")
            if not box:
                continue
            bl, bt, br, bb = box
            if max(rl, bl) < min(rr, br) and max(rt, bt) < min(rb, bb):
                rl = min(rl, bl)
                rt = min(rt, bt)
                rr = max(rr, br)
                rb = max(rb, bb)
        r[0], r[1], r[2], r[3] = rl, rt, rr, rb
        
    if win_rect:
        wl, wt, wr, wb = win_rect
        clamped = []
        for (l, t, r, b) in result_regions:
            cl = max(wl, l)
            ct = max(wt, t)
            cr = min(wr, r)
            cb = min(wb, b)
            if cr > cl and cb > ct:
                clamped.append([cl, ct, cr, cb])
        result_regions = clamped
        
    return merge_rectangles(result_regions, margin=0, merge_distance=10)

def execute_regional_ocr_crops(full_img, regions, win_left=0, win_top=0):
    """
    Executa WinRT OCR nas sub-regiões recortadas, monitorando se textos tocam a borda do crop.
    """
    t0 = time.time()
    regional_marks = []
    edge_touching_detected = False
    
    for r in regions:
        rl, rt, rr, rb = r
        crop_l = max(0, rl - win_left)
        crop_t = max(0, rt - win_top)
        crop_r = min(full_img.width, rr - win_left)
        crop_b = min(full_img.height, rb - win_top)
        
        if crop_r <= crop_l or crop_b <= crop_t:
            continue
            
        crop_img = full_img.crop((crop_l, crop_t, crop_r, crop_b))
        marks_in_crop = _ocr_candidates(crop_img)
        
        crop_w = crop_r - crop_l
        crop_h = crop_b - crop_t
        
        for m in marks_in_crop:
            box = m.get("box", [0, 0, 0, 0])
            # Checa se o texto toca a borda do crop (risco de corte de palavra)
            if (box[0] <= 8 and crop_l > 0) or (box[1] <= 8 and crop_t > 0) or \
               (box[2] >= crop_w - 8 and crop_r < full_img.width) or \
               (box[3] >= crop_h - 8 and crop_b < full_img.height):
                edge_touching_detected = True
                
            abs_box = [
                box[0] + crop_l + win_left,
                box[1] + crop_t + win_top,
                box[2] + crop_l + win_left,
                box[3] + crop_t + win_top
            ]
            cx = (abs_box[0] + abs_box[2]) // 2
            cy = (abs_box[1] + abs_box[3]) // 2
            
            regional_marks.append({
                "text": m.get("text", ""),
                "box": abs_box,
                "center": (cx, cy),
                "confidence": m.get("confidence", 1.0)
            })
            
    latency_ms = (time.time() - t0) * 1000.0
    return regional_marks, latency_ms, edge_touching_detected

def validate_structural_integrity(previous_marks, regional_new_marks, regions, edge_touching=False, win_rect=None):
    """
    Guarda Estrutural contra Reflow:
    Verifica se a atualização regional é estritamente segura ou se houve reflow/deslocamento
    que exige Full OCR imediato.
    """
    if edge_touching:
        return False, "edge_touching_text_detected"
        
    # Checagem de contagem prévia na mesma região:
    # Se uma região tinha N marcas antes e agora tem 0 sem explicação, ou se houve reflow de vizinhos
    prev_in_regions = 0
    for m in previous_marks:
        box = m.get("box") or [m["center"][0]-10, m["center"][1]-10, m["center"][0]+10, m["center"][1]+10]
        bl, bt, br, bb = box
        for (rl, rt, rr, rb) in regions:
            if max(bl, rl) < min(br, rr) and max(bt, rt) < min(bb, rb):
                prev_in_regions += 1
                break
                
    # Se a região era pequena mas causou divergência de elementos (ex: seleção que altera outros painéis)
    if prev_in_regions > 0 and len(regional_new_marks) == 0:
        return False, "disappeared_content_without_replacement"
        
    return True, "safe"

def reconcile_state_marks(previous_marks, regional_new_marks, regions):
    """
    Reconciliação com remoção estrita de marcas obsoletas.
    """
    t0 = time.time()
    preserved_marks = []
    
    for m in previous_marks:
        box = m.get("box") or [m["center"][0]-10, m["center"][1]-10, m["center"][0]+10, m["center"][1]+10]
        bl, bt, br, bb = box
        
        intersecta_alterada = False
        for (rl, rt, rr, rb) in regions:
            if max(bl, rl) < min(br, rr) and max(bt, rt) < min(bb, rb):
                intersecta_alterada = True
                break
                
        if not intersecta_alterada:
            preserved_marks.append(m)
            
    combined = list(preserved_marks)
    for nm in regional_new_marks:
        combined.append(nm)
        
    latency_ms = (time.time() - t0) * 1000.0
    return combined, latency_ms

def smart_regional_ocr_pipeline(full_img, previous_marks, dirty_regions, win_rect, max_area_pct=20.0):
    """
    Pipeline com Decisão Dupla:
    OCR Regional Ativo <==> (dirty_area <= 20%) AND (structural_validation == safe)
    Caso contrário: Fallback automático transparente para Full OCR!
    """
    t_start = time.time()
    win_w = win_rect[2] - win_rect[0]
    win_h = win_rect[3] - win_rect[1]
    win_area = max(1, win_w * win_h)
    
    # 1. Margem adaptativa baseada em linhas de texto
    margin = compute_adaptive_margin(previous_marks, default_margin=20)
    
    # 2. Expansão inteligente para containers de reflow (ex: Inspector da Unity)
    reflow_expanded = expand_for_reflow_containers(dirty_regions, win_rect=win_rect)
    
    # 3. Expansão para envolver marcas SoM existentes que tocam a borda
    final_regions = expand_with_intersecting_marks(reflow_expanded, previous_marks, win_rect=win_rect)
    
    # 4. Avalia critério 1: % de área da janela
    total_reproc_area = sum((r[2]-r[0])*(r[3]-r[1]) for r in final_regions)
    area_pct = (total_reproc_area / win_area) * 100.0
    
    if area_pct > max_area_pct:
        # Fallback 1: Área muito grande, Full OCR direto
        t_f0 = time.time()
        full_marks = _ocr_candidates(full_img)
        full_lat = (time.time() - t_f0) * 1000.0
        return {
            "mode": "fallback_full_ocr",
            "reason": f"area_threshold_exceeded_{area_pct:.1f}%",
            "marks": full_marks,
            "latency_ms": round(full_lat, 1),
            "area_pct": round(area_pct, 1),
            "fallback": True
        }
        
    # 5. Executa OCR nas sub-regiões
    reg_marks, crop_lat, edge_touching = execute_regional_ocr_crops(full_img, final_regions)
    
    # 6. Avalia critério 2: Guarda estrutural
    is_safe, struct_reason = validate_structural_integrity(previous_marks, reg_marks, final_regions, edge_touching, win_rect)
    
    if not is_safe:
        # Fallback 2: Guarda estrutural reprovou
        t_f0 = time.time()
        full_marks = _ocr_candidates(full_img)
        full_lat = (time.time() - t_f0) * 1000.0
        return {
            "mode": "fallback_full_ocr",
            "reason": f"structural_guard_{struct_reason}",
            "marks": full_marks,
            "latency_ms": round(full_lat, 1),
            "area_pct": round(area_pct, 1),
            "fallback": True
        }
        
    # 7. Reconciliação
    reconciled_marks, recon_lat = reconcile_state_marks(previous_marks, reg_marks, final_regions)
    total_lat = (time.time() - t_start) * 1000.0
    
    return {
        "mode": "regional_ocr_reconciled",
        "reason": "safe_and_within_threshold",
        "marks": reconciled_marks,
        "latency_ms": round(total_lat, 1),
        "area_pct": round(area_pct, 1),
        "fallback": False
    }

def compare_mark_sets(set_reconciled, set_full, iou_threshold=0.45):
    """
    Compara marcas reconciliadas com Ground Truth.
    """
    full_matched = [False] * len(set_full)
    reconciled_matched = [False] * len(set_reconciled)
    matched_pairs = []
    
    for i, rm in enumerate(set_reconciled):
        rb = rm.get("box", [0, 0, 0, 0])
        rtxt = rm.get("text", "").strip().lower()
        best_j = -1
        best_iou = 0.0
        for j, fm in enumerate(set_full):
            if full_matched[j]:
                continue
            fb = fm.get("box", [0, 0, 0, 0])
            ftxt = fm.get("text", "").strip().lower()
            
            inter_l = max(rb[0], fb[0])
            inter_t = max(rb[1], fb[1])
            inter_r = min(rb[2], fb[2])
            inter_b = min(rb[3], fb[3])
            
            if inter_r > inter_l and inter_b > inter_t:
                inter_area = (inter_r - inter_l) * (inter_b - inter_t)
                r_area = max(1, (rb[2] - rb[0]) * (rb[3] - rb[1]))
                f_area = max(1, (fb[2] - fb[0]) * (fb[3] - fb[1]))
                union_area = r_area + f_area - inter_area
                iou = inter_area / union_area
                if iou >= iou_threshold or (rtxt == ftxt and iou >= 0.25):
                    if iou > best_iou:
                        best_iou = iou
                        best_j = j
        if best_j != -1:
            full_matched[best_j] = True
            reconciled_matched[i] = True
            matched_pairs.append((rm, set_full[best_j]))
            
    perdidos = [set_full[j] for j in range(len(set_full)) if not full_matched[j]]
    falsos_ou_duplicados = [set_reconciled[i] for i in range(len(set_reconciled)) if not reconciled_matched[i]]
    total_elements = max(1, len(set_full))
    equivalencia = round((len(matched_pairs) / total_elements) * 100.0, 2)
    
    return {
        "total_full": len(set_full),
        "total_reconciled": len(set_reconciled),
        "matched": len(matched_pairs),
        "perdidos_count": len(perdidos),
        "falsos_count": len(falsos_ou_duplicados),
        "equivalencia_pct": equivalencia,
        "perdidos_sample": [p.get("text") for p in perdidos[:10]],
        "falsos_sample": [f.get("text") for f in falsos_ou_duplicados[:10]]
    }
