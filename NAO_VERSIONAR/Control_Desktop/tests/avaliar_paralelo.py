import urllib.request
import json
import time
import concurrent.futures

with open('test_cases.json') as f:
    test_cases = json.load(f)

url = "http://localhost:20128/v1/chat/completions"
headers = {
    "Authorization": "Bearer sk-f4a7880fa3f5b43a-9ikjjf-65c7e791",
    "Content-Type": "application/json"
}
SISTEMA = (
    "Voce recebe a descricao estruturada de uma tela de computador, capturada por "
    "OCR. Cada marca tem tag, tipo, texto e bbox [x, y, largura, altura] em pixels. "
    "Quando houver 'groups', cada grupo reune marcas que estao na mesma linha "
    "visual ('row') ou no mesmo bloco ('block').\n"
    "Responda a pergunta usando SOMENTE o que esta na descricao. "
    "Responda de forma minima: apenas o valor pedido, sem frase, sem explicacao."
)

def perguntar(corpo, pergunta):
    data = {
        "model": "oc/muse-spark-1.3-contributor-free",
        "messages": [
            {"role": "user", "content": f"{SISTEMA}\n\n<tela>\n{corpo}\n</tela>\n\nPergunta: {pergunta}"}
        ]
    }
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
    inicio = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read())
            texto = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            return texto, time.time() - inicio
    except Exception as e:
        return f"ERRO: {e}", time.time() - inicio

import re
def normalizar(valor):
    valor = str(valor or "").strip().lower()
    valor = re.sub(r"[^\w\s.:x/-]", "", valor)
    return re.sub(r"\s+", " ", valor).strip()
def acertou(resposta, esperado):
    a, b = normalizar(resposta), normalizar(esperado)
    return bool(a) and (a == b or b in a or a in b)

def evaluate_case(case):
    pergunta = case['pergunta']
    esperado = case['esperado']
    
    ans_sem, lat_sem = perguntar(case['sem_grafo'], pergunta)
    ans_com, lat_com = perguntar(case['com_grafo'], pergunta)
    
    return {
        'id': case['id'],
        'tipo': case['tipo'],
        'pergunta': pergunta,
        'esperado': esperado,
        'sem_grafo': {'resposta': ans_sem, 'acerto': acertou(ans_sem, esperado), 'latencia': lat_sem},
        'com_grafo': {'resposta': ans_com, 'acerto': acertou(ans_com, esperado), 'latencia': lat_com},
    }

print("Iniciando avaliacao paralela de 12 perguntas...")
results = []
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(evaluate_case, c) for c in test_cases]
    for i, future in enumerate(concurrent.futures.as_completed(futures)):
        res = future.result()
        results.append(res)
print(f"[{i+1}/24] Tipo: {res['tipo']} | Esperado: {res['esperado']} | Ans Sem: {res['sem_grafo']['resposta']} | Ans Com: {res['com_grafo']['resposta']}")

acertos_sem = sum(1 for r in results if r['sem_grafo']['acerto'])
acertos_com = sum(1 for r in results if r['com_grafo']['acerto'])
print("\n=== RESUMO ===")
print(f"Sem grafo: {acertos_sem}/{len(results)} ({acertos_sem/len(results)*100:.0f}%)")
print(f"Com grafo: {acertos_com}/{len(results)} ({acertos_com/len(results)*100:.0f}%)")

for tipo in set(r['tipo'] for r in results):
    t_res = [r for r in results if r['tipo'] == tipo]
    t_sem = sum(1 for r in t_res if r['sem_grafo']['acerto'])
    t_com = sum(1 for r in t_res if r['com_grafo']['acerto'])
    print(f"{tipo:12} - Sem: {t_sem}/{len(t_res)} | Com: {t_com}/{len(t_res)}")
