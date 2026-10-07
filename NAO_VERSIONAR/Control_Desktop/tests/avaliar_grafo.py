"""Avalia se o grafo de cena (IDEIAS/03, item 2) melhora a percepção do modelo.

Compara dois braços sobre as MESMAS telas e as MESMAS perguntas:

    sem_grafo  -> só a lista de marcas do SoM
    com_grafo  -> marcas + a lista de grupos (linhas e blocos)

Mede taxa de acerto, tokens, custo, latência e o recorte por tipo de pergunta.

Rode na máquina Windows, com o servidor de pé, para incluir telas reais:

    set ANTHROPIC_API_KEY=...
    python avaliar_grafo.py --sinteticas 12 --reais 6 --modelo claude-opus-5

Sem telas reais (qualquer sistema operacional):

    python avaliar_grafo.py --sinteticas 12 --reais 0

Cuidado com efeito de teto: se o modelo acerta tudo nos dois braços, a medição
não informa nada. Por isso o padrão é esforço baixo, e vale rodar também um
modelo menor - a diferença entre representações aparece onde o modelo tem menos
folga para raciocinar em cima de uma representação ruim.
"""

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.request

PRECOS = {  # US$ por milhão de tokens (entrada, saída)
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
SEM_ESFORCO = ("claude-haiku-4-5",)  # não aceitam output_config.effort


# ----------------------------------------------------------------- cenas
def _marca(tag, x, y, w, h, texto, tipo="text"):
    return {"tag": tag, "type": tipo, "text": texto,
            "bbox": [x, y, w, h], "center": [x + w // 2, y + h // 2]}


ROTULOS = ["Scripting Backend", "Api Compatibility", "Target Platform",
           "Color Space", "Graphics API", "Resolution", "Fullscreen Mode",
           "Company Name", "Product Name", "Bundle Version", "Texture Format",
           "Audio Sample Rate", "Render Pipeline", "Shadow Distance",
           "Anisotropic Textures", "VSync Count", "Lightmap Encoding"]
VALORES = ["Mono", ".NET Standard", "Windows", "Linear", "Direct3D11",
           "1920x1080", "Exclusive", "DefaultCompany", "MeuJogo", "0.1.4",
           "ASTC", "48000 Hz", "URP", "150", "Per Texture", "Every VBlank",
           "High Quality"]


def cena_sintetica(seed):
    """Painéis de formulário com rótulo+valor, verdade-terreno exata.

    Duas decisões deliberadas:

    Cada rotulo aparece NO MAXIMO UMA VEZ por cena. Rotulo repetido tornaria
    "qual o valor do campo X?" ambiguo e a pergunta, ingradeavel.

    O valor e sorteado SEM relacao semantica com o rotulo - "Target Platform"
    pode valer "DefaultCompany". Parece errado e e de proposito: com o par
    obvio, o modelo acerta do conhecimento de mundo sem ler a representacao, e
    a avaliacao mediria memoria em vez de percepcao. Emparelhamento arbitrario
    obriga a ler a tela.
    """
    rng = random.Random(seed)
    rotulos = rng.sample(ROTULOS, len(ROTULOS))
    valores = rng.sample(VALORES, len(VALORES))
    marcas, pares, tag, proximo = [], [], 1, 0
    altura = rng.choice([14, 16, 18])
    y = 60
    titulos = []
    for p in range(rng.randint(2, 3)):
        titulo = ["Player Settings", "Quality", "Graphics", "Audio"][p % 4]
        titulos.append((titulo, y))
        marcas.append(_marca(tag, 40, y, 140, altura, titulo)); tag += 1
        y += int(altura * 2.2)
        for _ in range(rng.randint(2, 4)):
            if proximo >= len(rotulos):
                break
            rot, val = rotulos[proximo], valores[proximo]
            proximo += 1
            gutter = rng.randint(int(altura * 2), int(altura * 6))
            largura = 8 * len(rot)
            marcas.append(_marca(tag, 60, y, largura, altura, rot)); tag += 1
            marcas.append(_marca(tag, 60 + largura + gutter, y,
                                 8 * len(val), altura, val)); tag += 1
            pares.append((rot, val, titulo))
            y += int(altura * 1.9)
        y += rng.randint(30, 60)
    return {"origem": f"sintetica#{seed}", "marks": marcas,
            "pares": pares, "titulos": titulos}


def cena_real(url, monitor="1"):
    """Captura uma tela de verdade pelo próprio servidor."""
    alvo = f"{url}/state?mode=som&monitor={monitor}&stable=1"
    with urllib.request.urlopen(alvo, timeout=60) as resposta:
        dados = json.loads(resposta.read())
    if not dados.get("marks"):
        raise RuntimeError(f"/state não devolveu marcas: modo={dados.get('mode')}")
    return {"origem": f"real:{dados.get('window', {}).get('process', '?')}",
            "marks": dados["marks"], "pares": None, "titulos": None}


# ------------------------------------------------------------- perguntas
def _vizinho_a_direita(marcas, alvo):
    """Oráculo INDEPENDENTE do agrupamento: vizinho imediato à direita.

    Regra deliberadamente mais simples que o group_marks (sem union-find, sem
    análise de outlier) para não medir o algoritmo contra ele mesmo.
    """
    ax, ay, aw, ah = alvo["bbox"]
    centro = ay + ah / 2
    candidatos = []
    for m in marcas:
        if m is alvo:
            continue
        mx, my, mw, mh = m["bbox"]
        if mx < ax + aw:
            continue
        if abs((my + mh / 2) - centro) > ah * 0.6:
            continue
        candidatos.append((mx, m))
    return min(candidatos)[1] if candidatos else None


def perguntas_sinteticas(cena, rng, quantas):
    itens = []
    pares = cena["pares"]
    for rot, val, _painel in rng.sample(pares, min(len(pares), quantas)):
        itens.append({"tipo": "associacao",
                      "pergunta": f'Qual é o valor do campo "{rot}"?',
                      "esperado": val})
    rot, val, painel = rng.choice(pares)
    itens.append({"tipo": "busca",
                  "pergunta": f'O texto "{val}" aparece nesta tela? Responda sim ou nao.',
                  "esperado": "sim"})
    presentes = {r for r, _v, _p in pares}
    ausentes = [r for r in ROTULOS if r not in presentes]
    if ausentes:
        itens.append({"tipo": "negativa",
                      "pergunta": f'Qual é o valor do campo "{rng.choice(ausentes)}"? '
                                  f'Se o campo nao existir nesta tela, responda "nao existe".',
                      "esperado": "nao existe"})
    quantos = sum(1 for _r, _v, p in pares if p == painel)
    itens.append({"tipo": "contagem",
                  "pergunta": f'Quantos pares de rotulo e valor existem no painel "{painel}"? '
                              f'Responda apenas o numero.',
                  "esperado": str(quantos)})
    return itens


def perguntas_reais(cena, rng, quantas):
    """Mesmo formato, com verdade vinda do oráculo geométrico independente."""
    marcas = [m for m in cena["marks"]
              if len(str(m.get("text") or "").strip()) >= 3
              and not str(m.get("text", "")).startswith("[Icon")]
    itens = []
    rng.shuffle(marcas)
    for alvo in marcas:
        if len(itens) >= quantas:
            break
        vizinho = _vizinho_a_direita(cena["marks"], alvo)
        if not vizinho or not str(vizinho.get("text") or "").strip():
            continue
        itens.append({"tipo": "associacao",
                      "pergunta": f'Qual texto aparece imediatamente a direita de '
                                  f'"{alvo["text"]}" na mesma linha?',
                      "esperado": vizinho["text"],
                      "conferir_a_mao": True})
    return itens


# ------------------------------------------------------------------ arms
def payload(cena, com_grafo):
    marcas = []
    for m in cena["marks"]:
        item = {k: m[k] for k in ("tag", "type", "text", "bbox") if k in m}
        for extra in ("bg", "fg"):
            if extra in m:
                item[extra] = m[extra]
        if com_grafo:
            for extra in ("group", "block"):
                if m.get(extra):
                    item[extra] = m[extra]
        marcas.append(item)
    corpo = {"marks": marcas}
    if com_grafo and cena.get("groups"):
        corpo["groups"] = cena["groups"]
    return json.dumps(corpo, ensure_ascii=False)


SISTEMA = (
    "Voce recebe a descricao estruturada de uma tela de computador, capturada por "
    "OCR. Cada marca tem tag, tipo, texto e bbox [x, y, largura, altura] em pixels. "
    "Quando houver 'groups', cada grupo reune marcas que estao na mesma linha "
    "visual ('row') ou no mesmo bloco ('block').\n"
    "Responda a pergunta usando SOMENTE o que esta na descricao. "
    "Responda de forma minima: apenas o valor pedido, sem frase, sem explicacao."
)


def perguntar(cliente, modelo, corpo, pergunta, esforco):
    kwargs = {
        "model": modelo,
        "max_tokens": 200,
        "system": SISTEMA,
        "messages": [{"role": "user",
                      "content": f"<tela>\n{corpo}\n</tela>\n\nPergunta: {pergunta}"}],
    }
    if modelo not in SEM_ESFORCO:
        kwargs["output_config"] = {"effort": esforco}
    inicio = time.time()
    resposta = cliente.messages.create(**kwargs)
    latencia = time.time() - inicio
    texto = "".join(b.text for b in resposta.content if b.type == "text").strip()
    return {"texto": texto, "latencia": latencia,
            "entrada": resposta.usage.input_tokens,
            "saida": resposta.usage.output_tokens}


def normalizar(valor):
    valor = str(valor or "").strip().lower()
    valor = re.sub(r"[^\w\s.:x/-]", "", valor)
    return re.sub(r"\s+", " ", valor).strip()


def acertou(resposta, esperado):
    a, b = normalizar(resposta), normalizar(esperado)
    return bool(a) and (a == b or b in a or a in b)


# ------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sinteticas", type=int, default=12)
    p.add_argument("--reais", type=int, default=0)
    p.add_argument("--servidor", default="http://127.0.0.1:7842")
    p.add_argument("--monitor", default="1")
    p.add_argument("--modelo", default="claude-opus-5")
    p.add_argument("--esforco", default="low", choices=["low", "medium", "high"])
    p.add_argument("--seed", type=int, default=20260923)
    p.add_argument("--saida", default="avaliacao_grafo.json")
    args = p.parse_args()

    try:
        import anthropic
    except ImportError:
        sys.exit("Falta o SDK: pip install anthropic")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Defina ANTHROPIC_API_KEY antes de rodar.")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from remote_control_server import group_marks, collect_groups

    rng = random.Random(args.seed)
    cenas = []
    for i in range(args.sinteticas):
        cenas.append(cena_sintetica(args.seed + i))
    for i in range(args.reais):
        try:
            cenas.append(cena_real(args.servidor, args.monitor))
            if i + 1 < args.reais:
                print(f"  tela real {i+1}/{args.reais} capturada - "
                      f"mude a tela e tecle Enter", end="")
                input()
        except Exception as exc:
            print(f"[aviso] tela real {i+1} falhou: {exc}")

    # o grafo e calculado uma vez por cena; os dois bracos veem a MESMA deteccao
    for cena in cenas:
        group_marks(cena["marks"])
        cena["groups"] = collect_groups(cena["marks"])

    itens = []
    for cena in cenas:
        gerador = perguntas_sinteticas if cena["pares"] else perguntas_reais
        for q in gerador(cena, rng, 3):
            q["cena"] = cena
            itens.append(q)

    cliente = anthropic.Anthropic()
    preco = PRECOS.get(args.modelo, (5.0, 25.0))
    registros = []
    print(f"\n{len(itens)} perguntas x 2 bracos = {len(itens)*2} chamadas "
          f"({args.modelo}, esforco {args.esforco})\n")

    for n, item in enumerate(itens, 1):
        linha = {"tipo": item["tipo"], "pergunta": item["pergunta"],
                 "esperado": item["esperado"], "origem": item["cena"]["origem"],
                 "conferir_a_mao": item.get("conferir_a_mao", False)}
        for braco, com in (("sem_grafo", False), ("com_grafo", True)):
            corpo = payload(item["cena"], com)
            try:
                r = perguntar(cliente, args.modelo, corpo, item["pergunta"], args.esforco)
            except anthropic.RateLimitError as exc:
                espera = int(exc.response.headers.get("retry-after", "20"))
                time.sleep(espera)
                r = perguntar(cliente, args.modelo, corpo, item["pergunta"], args.esforco)
            r["acerto"] = acertou(r["texto"], item["esperado"])
            r["custo"] = r["entrada"]/1e6*preco[0] + r["saida"]/1e6*preco[1]
            linha[braco] = r
        registros.append(linha)
        marca = {True: "+", False: "-"}
        print(f"  {n:3d}/{len(itens)} [{item['tipo'][:11]:11}] "
              f"sem:{marca[linha['sem_grafo']['acerto']]} "
              f"com:{marca[linha['com_grafo']['acerto']]}  {item['pergunta'][:52]}")

    relatorio(registros, args)
    with open(args.saida, "w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False, indent=1)
    print(f"\nBruto em {args.saida}")


def relatorio(registros, args):
    def agregar(sub):
        out = {}
        for braco in ("sem_grafo", "com_grafo"):
            dados = [r[braco] for r in sub]
            out[braco] = {
                "acerto": sum(d["acerto"] for d in dados) / max(1, len(dados)),
                "entrada": statistics.mean(d["entrada"] for d in dados),
                "saida": statistics.mean(d["saida"] for d in dados),
                "latencia": statistics.median(d["latencia"] for d in dados),
                "custo": sum(d["custo"] for d in dados),
            }
        return out

    print("\n" + "=" * 76)
    print(f"RESULTADO  ({args.modelo}, esforco {args.esforco}, {len(registros)} perguntas)")
    print("=" * 76)
    g = agregar(registros)
    print(f"{'':12} {'acerto':>8} {'tok entrada':>12} {'tok saida':>10} "
          f"{'latencia':>10} {'custo US$':>10}")
    for braco in ("sem_grafo", "com_grafo"):
        d = g[braco]
        print(f"{braco:12} {d['acerto']*100:7.1f}% {d['entrada']:12.0f} "
              f"{d['saida']:10.0f} {d['latencia']:9.2f}s {d['custo']:10.4f}")
    delta = (g["com_grafo"]["acerto"] - g["sem_grafo"]["acerto"]) * 100
    extra = (g["com_grafo"]["entrada"] / max(1, g["sem_grafo"]["entrada"]) - 1) * 100
    print(f"\n  delta de acerto: {delta:+.1f} ponto(s)   custo extra de token: {extra:+.0f}%")

    print("\npor tipo de pergunta:")
    tipos = sorted({r["tipo"] for r in registros})
    print(f"  {'tipo':14} {'n':>3} {'sem grafo':>10} {'com grafo':>10} {'delta':>8}")
    for tipo in tipos:
        sub = [r for r in registros if r["tipo"] == tipo]
        d = agregar(sub)
        dl = (d["com_grafo"]["acerto"] - d["sem_grafo"]["acerto"]) * 100
        print(f"  {tipo:14} {len(sub):3d} {d['sem_grafo']['acerto']*100:9.0f}% "
              f"{d['com_grafo']['acerto']*100:9.0f}% {dl:+7.0f}")

    reais = [r for r in registros if r["origem"].startswith("real")]
    if reais:
        sub = agregar(reais)
        print(f"\nsomente telas reais ({len(reais)} perguntas): "
              f"sem {sub['sem_grafo']['acerto']*100:.0f}%  "
              f"com {sub['com_grafo']['acerto']*100:.0f}%")
        print("  (a verdade destas veio de oraculo geometrico; confira a mao "
              "as marcadas em 'conferir_a_mao' no JSON)")

    print("\nCOMO LER: se o delta de acerto nao cobrir os +72% de token, o grafo")
    print("nao deve ficar sempre ligado - use SOM_GROUPS=0 e ative por requisicao.")


if __name__ == "__main__":
    main()
