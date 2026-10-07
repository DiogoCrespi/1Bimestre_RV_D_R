# Ideia 03 — Percepção de Tela: o que medir, o que descartar e o que perguntar

> **Status:** Ideia
> **Data:** 2026-09-23
> **Base:** `inspect_screen_som()` em `remote_control_server.py` + a assinatura de estado da IDEIAS/02

---

## O Estado Atual: o que o `/som` faz

Duas passagens independentes sobre a mesma captura, sem nenhum modelo aprendido —
tudo CPU, tudo clássico.

**Texto.** WinOCR nativo sobre a imagem. Se a colheita vier pobre (`< 6`
candidatos ou `< 40` caracteres), entra o modo *boost*: reprocessa variantes
(upscale, contraste) e funde os candidatos por sobreposição.

**Ícones.** Canny com limiares derivados da mediana do brilho (`0.66×med` a
`1.33×med` — auto-Canny), `MORPH_CLOSE` para fechar contorno quebrado, depois
`connectedComponentsWithStats`. Cada componente passa por um filtro de
plausibilidade — tamanho entre ~9 e ~64 px escalado por DPI, razão de aspecto
0,35–2,85, sem colidir com texto já detectado. Quatro heurísticas viram um score
(densidade de pixels, aspecto, desvio-padrão do recorte, tamanho mínimo) e
`confidence = score/4`. Com o limiar padrão em 0,85, na prática **exige as quatro**.

Depois vem o rastreador de tags estáveis e o desenho dos badges.

### O que ele entrega e o que ele joga fora

| Entrega | Descarta |
|---|---|
| `tag`, `type`, `text`, `bbox`, `center` | **cor** (nenhuma marca carrega um único valor) |
| lista plana de elementos | **hierarquia** (o que está dentro do quê) |
| posição exata em pixel | **fonte, peso, espaçamento** |
| | **função** (o que aquilo faz quando clicado) |

Isso não é defeito: ele foi feito para **agir** (clicar na tag 7), não para
**descrever**. Mas é exatamente por isso que ele não resolve os problemas abaixo.

---

## O Vocabulário do Problema

O paper [*What's Missing in Screen-to-Action?*](https://arxiv.org/abs/2604.06995)
(UILoop, 2026) decompõe a falha de agente de GUI em três erros. É o vocabulário
certo para medir o que temos:

| Erro | O que é | `/som` hoje |
|---|---|---|
| **Locate** | achar o elemento na tela | resolve |
| **Lingualize** | dizer o que aquilo é | não faz |
| **Leverage** | saber como usar | não faz |

O `[Icon 42]` que o servidor emite hoje é um erro de Lingualize em estado puro: o
agente recebe um retângulo numerado e zero informação sobre o conteúdo. Ele sabe
*onde clicar* e não sabe *no quê*.

---

## Ideia A — Tela como SVG: objetivo certo, formato errado

A intenção é boa: trocar estimativa por **valor exato**. É o mesmo buraco que a
IDEIAS/01 aponta na imagem de referência, agora do lado da tela — o modelo sabe
"roxo escuro", não sabe `#1A0533`.

Mas a conta não fecha. Mesma tela de 1920×1080:

| Representação | Tokens |
|---|---|
| **Imagem** (tokens visuais, `⌈w/28⌉×⌈h/28⌉`) | **2691** |
| SoM atual, 60 marcas | ~1080 |
| **Abstração rica** (cor + papel + pai), 60 elementos | **~1695** |
| SVG vetorizado, ~1500 paths | ~45.000 *(estimativa)* |
| SVG vetorizado, ~4000 paths | ~150.000 *(estimativa)* |

As duas últimas linhas são estimativa de ordem de grandeza (caracteres ÷ 4, path
médio de 120–150 chars), não medição. Mas a conclusão não depende da precisão:
vetorizar custa **20 a 50× mais do que simplesmente mandar a imagem**.

**O ponto central: a imagem já é a compressão.** 2691 tokens para a tela inteira é
absurdamente barato. Vetorizar é *descomprimir* — troca um formato denso por um
esparso e paga por isso. E o antialiasing de texto, que é onde a UI toda tem
borda, é justamente o que explode a contagem de paths.

A literatura confirma por outro caminho. O [LLM4SVG](https://arxiv.org/abs/2412.11102)
(CVPR 2025) precisou criar *tokens semânticos aprendidos* para tags e atributos
justamente porque SVG como texto puro é caro demais — e mesmo assim topa em 2048
tokens. O [VGBench](https://arxiv.org/pdf/2407.10972) chega a apontar que LLMs se
saem melhor em **TikZ e Graphviz do que em SVG**: SVG é um meio ruim para modelo,
não um meio bom.

### O que fazer no lugar

A resposta não é comprimir o SVG — é **nunca gerar o SVG**. O detector já conhece
cada caixa; basta emitir a cor daquela região junto com ela:

```json
{
  "tag": 12, "role": "button", "text": "Build Settings",
  "box": [820, 140, 96, 18],
  "fg": "#F3E8FF", "bg": "#7B2FBE",
  "parent": 4
}
```

Custo: uma chamada de `ImageStat` ou um k-means de 3 clusters no recorte —
microssegundos por marca. Resultado: **1695 tokens, mais barato que a imagem**, e
carregando o que a imagem não carrega.

"Compressão sem perder a lógica" já está embutido nisso: não se comprime, **não se
representa o que não importa**. Um gradiente de fundo vira
`{"bg":"linear","from":"#1A0533","to":"#2C0F5E"}`, não 4000 paths.

---

## Ideia B — Grafo de cena: certo, e é o que falta

Concordo com a ideia, mudando só a origem: o grafo não deve sair do SVG, deve sair
do **agrupamento das detecções que já existem**.

| Relação | Regra geométrica |
|---|---|
| pai/filho | caixa A contida em B |
| irmãos | mesma coluna ou linha, dentro de uma tolerância |
| grupo | gap entre caixas abaixo de um limiar |

Vale saber que no Windows esse grafo **já existe de graça** para app convencional:
é a árvore UIA que o `inspect_window_uia()` lê. O buraco é exatamente onde o
projeto vive — Unity, jogo, Electron mal marcado, canvas. Aí o grafo precisa ser
inferido da geometria.

É o que transforma "60 caixas soltas" em "um Inspector com 3 seções, cada uma com
N campos" — ou seja, é o que ataca o **Lingualize** sem precisar de modelo de
legenda.

---

## Ideia C — Ecolocalização: percepção ativa (a melhor das três)

E não pelo motivo óbvio. Realçar borda e ícone é só pré-processamento — o Canny já
faz isso.

O que a ecolocalização tem de especial é ser **percepção ativa**: o morcego não
espera o mundo emitir sinal, ele **emite e escuta o diferencial**. O análogo na
tela não é realçar pixel; é **perturbar e observar**.

E há uma pergunta que nenhuma análise estática de screenshot responde:

> **O que aqui é clicável?**

Um retângulo colorido e um botão são pixels idênticos. Mas o botão **reage ao
hover**.

As duas peças já existem no servidor — a ação `hover` e a
`compute_state_signature()`. O ping seria:

```
assinatura de repouso
  → hover(x, y)
  → assinatura
  → diff

célula mudou  ⇒  ali existe elemento interativo
                 e o bbox dele é a região que mudou
```

Resolve o **Leverage** sem modelo, sem OCR, sem GPU.

Varrer a tela inteira assim é caro. Mas é **barato para confirmar um candidato
duvidoso** — que é justamente onde o score de 4 heurísticas erra. Um ícone com
`confidence 0.75` (3 de 4) hoje é silenciosamente descartado; com um ping de hover
dava para decidir.

---

## Detecção de Movimento: a escada clássica, e por que é a ferramenta errada aqui

### Como se faz

1. **Diferença de quadros** — `|Iₜ − Iₜ₋₁| > limiar`. Trivial, e acusa mudança de
   iluminação como se fosse movimento.
2. **Subtração de fundo** (MOG2, KNN) — modela a *distribuição* de cada pixel ao
   longo do tempo; pixel fora da distribuição é primeiro plano. Aguenta variação
   gradual de luz, mas exige **câmera fixa**.
3. **Fluxo óptico** — parte da hipótese de constância de brilho,
   `I(x,y,t) = I(x+dx, y+dy, t+dt)`. Expandindo em Taylor chega em
   `Iₓu + I_yv + Iₜ = 0`: **uma equação, duas incógnitas**. É o *problema da
   abertura* — olhando por um buraquinho, uma borda deslizando parece se mover só
   perpendicularmente. Resolver exige restrição extra: Lucas-Kanade assume fluxo
   constante numa vizinhança e resolve por mínimos quadrados; Horn-Schunck impõe
   suavidade global.
4. **Aprendido** — o [RAFT](https://www.emergentmind.com/topics/optical-flow-raft)
   monta um **volume de correlação 4D** `H×W×H×W` (cada pixel de um quadro contra
   *todos* do outro) e refina o fluxo iterativamente com uma GRU. Estado da arte,
   custo **quadrático** na resolução.

### Por que nada disso serve para tela

Tela não tem câmera tremendo, não tem iluminação variando, não tem oclusão. E,
principalmente:

> **O sistema operacional já sabe exatamente o que mudou.**

A [Desktop Duplication API](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api)
expõe [`GetFrameDirtyRects`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-getframedirtyrects)
— os retângulos que o compositor atualizou desde o último quadro — e
[`GetFrameMoveRects`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-getframemoverects),
regiões que foram **movidas**, com retângulo de destino e ponto de origem. É fluxo
óptico de precisão perfeita, entregue de graça pelo DWM.

Isso vale em dois lugares do projeto:

* **Na IDEIAS/02** — a grade 4×4 de dHash existe só para adivinhar *onde* a tela
  mudou. Os dirty rects respondem isso exatamente, sem hash nenhum.
* **No `/som`** — rodar OCR na tela inteira quando só um painel mudou é
  desperdício. Com dirty rects dá para reprocessar só o retângulo sujo.

**A má notícia:** o `dxcam` não expõe isso. Ele envolve a Desktop Duplication mas
entrega só `grab()`. Pegar os dirty rects significa falar com
`IDXGIOutputDuplication` direto via `ctypes`/`comtypes` — que é o tipo de coisa que
este arquivo já faz bem, mas é projeto, não uma linha.

---

## O Que os Projetos Estão Fazendo

| Projeto | O que faz | Relação com o nosso |
|---|---|---|
| [OmniParser](https://github.com/microsoft/omniparser) (Microsoft) | Detector **YOLOv9-E** de regiões interativas (jul/2026) + OCR + **modelo de legenda de ícone**. Melhor resultado no WindowsAgentArena | É o nosso `/som` com as heurísticas trocadas por modelos. Nosso Canny é a versão clássica do estágio 1 dele; a legenda de ícone é o que não temos |
| [UILoop](https://arxiv.org/abs/2604.06995) | Reformula "Screen→Action" para "Screen→Elementos→Action"; `UI Comprehension-Bench` com 26K exemplos | Fornece o vocabulário Locate/Lingualize/Leverage usado acima |
| [PixelRAG](https://github.com/StarTrail-org/PixelRAG) | Screenshot como unidade de recuperação, busca por similaridade visual | Tese oposta: não estruture, **indexe o pixel** |
| PreAct / EchoPath / SkillDroid | Memória de execução replayável | Virou a IDEIAS/02 |

**A tendência é clara: ninguém está indo para representação vetorial.** O movimento
é *detector aprendido + descrição semântica*, mantendo o pixel como entrada.

---

## Ordem de Ataque

| # | O quê | Esforço | Retorno |
|---|---|---|---|
| 1 | **Cor nas marcas do SoM** | pequeno | **FEITO** — ver abaixo |
| 2 | **Grafo por contenção/alinhamento** | médio, pura geometria | **FEITO** — ver abaixo |
| 3 | **Ping de hover** seletivo | pequeno | **FEITO** — ver abaixo |
| 4 | **Dirty rects via `IDXGIOutputDuplication`** | projeto de backend | **CONGELADO** — ver abaixo |
| 5 | **SVG** | — | **não fazer**; a intenção vira os itens 1 e 2 |

Ordem revisada: o grafo subiu para 2º e os dirty rects caíram para 4º. Motivo na
seção de ressalvas — o flip model enfraquece justamente o caso da Unity, e o
grafo é matemática pura, sem API de sistema e sem efeito colateral.

---

## Ressalvas que mudaram o plano

**Dirty rects inflados pelo flip model.** Em app acelerado por hardware
(`DXGI_SWAP_EFFECT_FLIP_DISCARD` — Chrome, Electron, Flutter, WPF moderno e
**Unity**), o DWM frequentemente invalida a superfície inteira da janela a cada
swap de buffer, mesmo que só um cursor de texto tenha piscado. O dirty rect vem
como o retângulo todo da janela, não o controle. Isso enfraquece o item 4
exatamente no app que mais importa aqui, e é por isso que ele desceu na lista.
Continua valendo para janela composta pelo DWM (diálogo, Explorer, menu), só não
é a bala de prata que parecia.

**Cursor de software nos dirty rects — risco menor do que parece.** A Desktop
Duplication **não desenha o ponteiro na imagem**: entrega posição e forma
separados, via `PointerPosition` e `GetFramePointerShape`. Movimento de mouse,
portanto, não suja a imagem. A exceção é adaptador ou driver WDDM sem cursor de
hardware (display virtual, VM, alguns cenários remotos): aí o ponteiro *é*
desenhado na imagem, `PointerPosition` sinaliza que não há ponteiro separado, e
todo movimento vira retângulo sujo. É caso de borda, não o caminho normal.

**Hover trava cursor.** Além de abrir tooltip e menu, janela Unity/DirectX que
usa `SetCapture` ou `ClipCursor` pode prender o ponteiro. O ping precisa
restaurar a posição anterior e nunca rodar durante replay de skill.

**Hover tem latência de animação.** Botão moderno não muda na hora — transição
de 150 a 300 ms. O ping precisa de debounce antes de capturar a assinatura, o
que com 10–15 candidatos duvidosos vira 2 a 4,5 s. Por isso o item 3 é
**seletivo**: só candidatos de score limítrofe (`0,65 ≤ confidence < 0,85`),
nunca varredura de tela.

---

## Item 1 — Entregue

`region_palette()` em `remote_control_server.py`. Toda marca do `/som` passa a
carregar `bg`, `fg`, `contrast` e `bg_share`.

Três coisas que os testes forçaram a mudar, e que valem registro porque são o
mesmo erro em três disfarces:

1. **Quantizador de octree não devolve cor que existe.** A primeira versão usava
   `quantize(FASTOCTREE)` e respondia `#7A2DBD` para um botão `#7B2FBE` — o
   centroide do cluster. Seria repetir o erro do JPEG por outro caminho. Agora
   conta as cores exatas com `getcolors()`.
2. **`thumbnail()` reamostra com BICUBIC e inventa cor.** A subamostragem de
   região grande passou a usar `NEAREST`, que só descarta pixel, nunca mistura.
3. **A captura do SoM ia e voltava por JPEG 95.** `capture_screen_fast()`
   codificava a tela e `Image.open()` decodificava de volta, só para ter um
   `PIL.Image` — custando alguns ms por chamada e **adulterando toda cor lida**.
   Trocado por `capture_raw_pil_image()`, que já existia e não tem perda.

**Precisão medida.** O fundo sai exato (`#7B2FBE` de um botão chapado, com
`bg_share` 1.0) — e é o caso que importa, porque é a cor de fundo que separa
perigo de link e de campo desabilitado. A frente de texto antialiasado é
aproximada por natureza: numa fonte de 11 px a haste é fina demais para sobrar
pixel na cor pura, e `#F3E8FF` volta como `#E6DBF3`. Perto, não igual — está
documentado na função para ninguém usar `fg` como cor de projeto.

**Custo:** 2,3 ms para 60 marcas. Desligável com `SOM_COLORS=0`.

---

## Item 2 — Entregue

`group_marks()` e `collect_groups()`. Cada marca ganha `group` (linha) e `block`;
o `/state` passa a trazer a lista `groups`.

**A contenção não serve aqui, e isso mudou o desenho.** As marcas do SoM são
todas folhas — texto e ícone de 9 a 64 px. Nenhum painel é detectado, então
quase nenhuma marca está dentro de outra e uma árvore por contenção pura não
produziria praticamente nada. O que existe de verdade numa tela é **alinhamento**.
São duas passagens de union-find: marcas viram linhas, linhas viram blocos.

**Largura de gutter não é sinal confiável.** A primeira versão quebrava linha
por um vão fixo em alturas de fonte, e falhava dos dois lados: apertado (1,6)
separava `Scripting Backend` de `Mono`; folgado juntava uma barra de menu
inteira. Não existe número certo, porque o gutter varia de formulário para
formulário.

O sinal que funciona é **o vão destoar dos outros vãos da mesma linha**. Dentro
de cada faixa horizontal, quebra-se onde `gap > 2,5 × mediana dos vãos daquela
linha`, com piso de uma altura de fonte e teto absoluto de 12. Medido: com o
**mesmo vão de 80 px**, junta em `Resolution  1920x1080` (vão único, não destoa
de nada) e quebra em `File Edit View | Play Pause` (vãos de 10 px ao redor, os
80 destoam). Um limiar fixo não consegue os dois casos.

**Custo:** 8 ms de CPU para 200 marcas. Desligável com `SOM_GROUPS=0`.

---

## O Grafo Melhora Mesmo a Percepção? — o que foi medido

Até aqui o grafo tinha sido validado contra fixtures escritas à mão por mim, o
que é circular. Abaixo, medição contra verdade-terreno gerada.

**Método.** Um gerador produz cenas de UI sorteadas em cinco famílias —
formulário, barra de ferramentas, tabela, árvore indentada e diálogo —
distribuídas em painéis lado a lado e empilhados. A verdade-terreno sai do
gerador (ele sabe qual rótulo pertence a qual campo porque foi ele que os
posicionou), não de inspeção do resultado. A métrica é **par**: para cada par
de marcas, a verdade diz se pertencem à mesma linha lógica e o algoritmo diz se
os agrupou. Par é a unidade certa porque é exatamente o que o agrupamento
promete — "este rótulo vai com este campo".

### Resultado

| Condição | Precisão | Recall | F1 |
|---|---|---|---|
| Entrada limpa | 1,000 | 0,997 | **0,999** |
| + jitter de caixa (±2 px) | 0,999 | 0,997 | 0,998 |
| + baseline desalinhada (±3 px) | 0,998 | 0,997 | 0,998 |
| + 10% de marcas perdidas | 1,000 | 0,950 | 0,974 |
| + ícones espúrios (3/cena) | 0,985 | 0,996 | 0,991 |
| + modal flutuante | 0,997 | 0,998 | 0,997 |
| **Tudo junto, layout denso** | **0,971** | **0,936** | **0,953** |

**O 0,999 da primeira linha não vale nada** — mede o algoritmo contra um mundo
que ele já pressupõe. O número honesto é o da última linha: **F1 0,953 sob ruído
combinado.**

Densidade de layout não afeta: de 60 px a 4 px de folga entre painéis o F1 fica
em 0,998. Agrupar por sobreposição vertical das próprias marcas é indiferente a
quão perto os painéis estão.

### Os dois modos de falha reais

1. **Marca perdida no meio quebra a linha** (recall 0,950). Se o OCR não lê o
   elemento do meio, os dois extremos ficam longe demais e a linha se parte.
2. **Ícone espúrio é absorvido** pela linha vizinha (precisão 0,985).

### Os limiares estão ajustados ao próprio teste?

Varredura em 300 cenas com ruído combinado:

| Parâmetro | Resposta |
|---|---|
| `SOM_ROW_GAP_MAX` | pico exatamente no padrão 12 (F1 0,945); platô largo de 9 a 18 |
| `SOM_ROW_OVERLAP` | pico em **0,6**, não no 0,5 que eu tinha posto — precisão 0,969 contra 0,944. **Padrão alterado.** |
| `SOM_ROW_SPLIT_RATIO` | **resposta plana** de 1,5 a 10. O benchmark sub-testa este parâmetro: as famílias geradas têm vãos uniformes, então quase nunca precisam de quebra por outlier. |

Platôs largos, não picos estreitos: os parâmetros **não estão no fio da navalha**.
Mas o `SPLIT_RATIO` fica sem evidência — quem o exercita é o caso feito à mão
(`File Edit View | Play Pause`), não o gerador.

### Correção: o grafo CUSTA token, não economiza

Uma versão anterior deste documento afirmava que o agrupamento era "economia de
token, não gasto". **Medido, é falso.** Por cena, em média:

| | Tokens |
|---|---|
| marcas sem grupo | 425 |
| marcas + grupos | **729 (+72%)** |
| linhas legíveis entregues | 4,8 por cena |
| custo por linha entregue | 63 tokens |

A primeira medição deu +81%; removi duplicação óbvia do meu próprio formato (o
bloco repetia as tags que já estavam nas linhas e nas marcas) e caiu para +72%.
Continua sendo gasto. Se compensa depende de o modelo usar melhor a linha
pronta do que as caixas soltas — e **isso este experimento não mede**.

### Sobrecarga contra tamanho da tela — e a ativação seletiva

Medido em 300 cenas, sobre o payload enxuto que a avaliação envia:

| Marcas | Linhas entregues | Sem grafo | Com grafo | Extra | Tokens por linha |
|---:|---:|---:|---:|---:|---:|
| 9 | 2,2 | 159 | 307 | +94% | 66,5 |
| 21 | 5,5 | 386 | 744 | +93% | 65,1 |
| 34 | 8,8 | 638 | 1200 | +88% | 63,7 |
| 60 | 16,0 | 1094 | 2112 | +93% | 63,6 |

A sobrecarga é **praticamente constante, perto de +90%**, e o custo por linha
entregue é estável em ~64 tokens. (O número de +72% citado antes vinha de uma
linha de base mais gorda, com todos os campos da marca; sobre o payload enxuto
é ~90%. As duas medições estão certas, medem coisas diferentes.)

**O caso que decide a ativação seletiva:** em 20 das 300 cenas — árvore de
projeto, itens soltos — o agrupamento **não produziu nenhuma linha** e ainda
assim os grupos custavam **+42% de token por zero linha entregue**.

Isso é corrigível sem saber nada sobre o modelo, e foi corrigido:
`SOM_GROUPS_MIN_ROWS` (padrão 1) suprime o agrupamento quando ele não gera
linha legível. Calcular continua custando 8 ms; o que se economiza é o envio.
A supressão fica no `group_marks`, e não na montagem da lista, porque se as
marcas mantivessem `block` apontando para um grupo não enviado sobraria
referência morta no payload. Medido depois: árvore vai de 125 para 125 tokens
(sobrecarga zero), formulário continua entregando seus grupos.

Suprimir **mais** que o caso nulo — exigir 3 linhas, 5 linhas — depende de
saber quanto acerto cada linha compra, e isso é a avaliação com modelo.

### A avaliação com modelo: executada — e deu teto

Rodada com 24 perguntas sobre cenas sintéticas, por um modelo servido via
router local:

```
Sem grafo: 24/24 (100%)
Com grafo: 24/24 (100%)

associacao  sem 12/12 | com 12/12
busca       sem  4/4  | com  4/4
contagem    sem  4/4  | com  4/4
negativa    sem  4/4  | com  4/4
```

**Efeito de teto, exatamente o risco previsto.** E vale ser preciso sobre o que
isso significa: 100% nos dois braços **não é evidência de que o grafo não
ajuda** — é evidência de que *este teste não conseguiu medir*. Com acerto
máximo nos dois lados, o poder estatístico para detectar diferença é
essencialmente zero. "Nenhuma evidência de benefício" e "evidência de nenhum
benefício" são coisas diferentes, e só a primeira foi obtida.

Limites do que foi rodado: cenas sintéticas apenas (nenhuma tela real de
Windows entrou), um único modelo, 24 perguntas, todas de formulário e painel.

**A decisão de desligar por padrão continua certa**, mas pelo motivo correto:
o custo é **medido e certo** (~+90% de token); o benefício é **desconhecido**.
Pagar custo certo por benefício desconhecido é mau negócio.

`SOM_GROUPS` passou a **0**. Liga por requisição com `?groups=1` no `/state`,
`{"groups": true}` no POST, ou `SOM_GROUPS=1` no ambiente.

**A implementação fica.** O teste não cobriu o que motivaria o grafo: tabela,
grade densa de planilha, tela onde a associação rótulo↔valor é ambígua por
geometria. É lá que ele deve ser ligado, e é lá que uma avaliação futura teria
chance de sair do teto.

---

## Item 3 — Entregue

`hover_probe()`. Descobre quais candidatos duvidosos **reagem ao ponteiro** —
a pergunta que nenhuma análise estática de screenshot responde, porque um
retângulo colorido e um botão são os mesmos pixels.

**Desligado por padrão** (`SOM_HOVER_PROBE=0`), e de propósito: diferente de
todo o resto do `/som`, isto mexe no mouse.

### Salvaguardas, todas verificadas

| Salvaguarda | Como |
|---|---|
| Nunca varredura global | só candidatos de confiança intermediária (`0,65 ≤ conf < 0,85`) |
| Teto de sondagens | `SOM_HOVER_MAX_PROBES` (8) |
| Orçamento de tempo | `SOM_HOVER_BUDGET_MS` (4 s), com corte no meio |
| Espera de animação | `SOM_HOVER_SETTLE_MS` (220 ms) — transição de botão leva 150–300 ms |
| Cursor devolvido | sempre, inclusive ao abortar ou levantar exceção (`finally`) |
| Cursor preso detectado | confere `GetCursorPos` depois de mover; se a janela prendeu via `SetCapture`/`ClipCursor`, **aborta** em vez de medir lixo |
| Nunca durante replay | flag por thread ligada em `run_skill`; sondar entre a verificação e o clique quebraria o passo |

### A cegueira do dHash apareceu de novo — e aqui seria fatal

A primeira versão media só `dHash` do recorte. No teste, **nenhum** botão foi
detectado como interativo. Motivo: botão que clareia no hover mantém
exatamente a mesma borda, e dHash não move **um bit**. É a mesma lição do item
1 ("dHash enxerga borda, não cor"), e aqui ela seria fatal — o detector
declararia inerte justamente o botão que mais reage.

A assinatura passou a ter **dois canais**: dHash para mudança de *forma*
(contorno de foco, sublinhado de link) e média de cor para mudança de
*preenchimento*. Reação é qualquer um dos dois passar do limiar.

### Validado em Windows real

| Caso | ΔForma | ΔCor | Resultado |
|---|---:|---:|---|
| Botão flat (só muda cor) | 1 bit | 14,0 | detectado |
| Botão com transição CSS 200 ms | 1 bit | 23,4 | detectado |
| Botão Fechar do Chrome | 0 bits | **190,3** | detectado |
| Botão falso (visual, sem evento) | 0 | 0,0 | inerte |
| Texto puro | 0 | 0,0 | inerte |
| Barra de tarefas vazia | 0 | 0,0 | inerte |
| Cursor travado (`ClipCursor`) | — | — | bloqueio detectado em 179 ms |

**Falso positivo: 0,0%.** E o achado que confirma o canal de cor: nos botões
que só mudam de preenchimento o dHash moveu **0 a 1 bit**. Sem a média de cor,
a taxa de falso negativo seria de **100%** — o método inteiro não funcionaria.

**Tempo de acomodação, medido:** 50 ms perde o início da animação (Δ zero),
100 ms fica no limiar, 150–220 ms é o pico. Padrão ajustado de 220 para
**180 ms**, que pega a transição inteira e poupa ~25% do tempo. Um lote de 6
candidatos custa ~1,1 s.

Limiares ajustados pelos números do campo: `SETTLE_MS` 180, `MAX_PROBES` 6,
`MIN_DELTA` 2 (estático mediu 0 bits), `MIN_COLOR` 6,0, `PAD` 6.

### O bug que tornava a sondagem um no-op silencioso

O `.env` define `SOM_ICON_MIN_CONFIDENCE=0.85`. A confiança do detector é
`round(score / 4.0, 2)`, ou seja, quantizada em `{0, 0,25, 0,5, 0,75, 1,0}` —
com corte em 0,85, **só ícone de 1,0 entrava no SoM**. E a faixa de sondagem
era `[0,65, 0,85)`. Interseção **vazia**: a sondagem nunca via um candidato
sequer, e devolvia `probed: 0` sem erro.

A correção não foi mexer no número, foi corrigir a premissa: **a dúvida mora
abaixo do limiar de aceite**, e aqueles candidatos eram descartados antes de
existir. Agora `inspect_screen_som(include_candidates=True)` — só no caminho
da sondagem, nunca no `/state` normal — recupera os ícones reprovados como
`type: "icon_candidate"`, e o teto da faixa subiu para 1,0. Quem tirou 4 de 4
não precisa de sondagem; quem tirou 3 precisa, e agora chega lá.

### Cegueira a tooltip — limitação aceita

O balão de ajuda surge a 20–40 px do cursor, fora do `HOVER_PAD` de 6 px. Se o
botão em si não muda de estilo, a sondagem o declara inerte. Aumentar o *pad*
para alcançar o tooltip traria elemento vizinho para dentro do recorte e
geraria falso positivo. Fica como cegueira conhecida: a sondagem responde
"este elemento reage?", não "algo aconteceu na tela?".

### Escape do cursor confinado

Detectado o travamento, `ClipCursor(NULL)` solta o confinamento por
**retângulo** — sem isso a restauração do ponteiro no `finally` falha em
silêncio. **Não resolve `SetCapture`**, que é por thread e só o dono libera.
Fica atrás de `SOM_HOVER_UNCLIP` (ligado) porque desconfinar por conta própria
atrapalha jogo que confina o cursor de propósito.

### Uso

```
POST /hover_probe  {"force": true, "max_probes": 4}
```

Sem `marks` no corpo, usa o último snapshot do `/state`. Anota `interactive` e
`hover_delta` nas marcas sondadas.

---

### Harness da avaliação

`avaliar_grafo.py` na raiz. Dois braços sobre as mesmas telas e as mesmas
perguntas, medindo acerto, tokens, custo, latência e o recorte por tipo de
pergunta (associação, busca, contagem, negativa).

**Não foi executado**: este contêiner não tem credencial de API, e — mais
importante — não consegue capturar tela real de Windows nem rodar o `winocr`.
A avaliação pertence à máquina onde o servidor roda.

Decisões de desenho que o ensaio seco forçou:

* **Rótulo único por cena.** Rótulo repetido torna "qual o valor do campo X?"
  ambíguo e a pergunta, ingradeável.
* **Valor deliberadamente arbitrário.** `Target Platform` pode valer
  `DefaultCompany`. Parece errado e é de propósito: com o par óbvio o modelo
  acerta do conhecimento de mundo sem ler a representação, e a avaliação
  mediria memória em vez de percepção.
* **Oráculo geométrico independente** para as telas reais — vizinho imediato à
  direita, regra deliberadamente mais simples que o `group_marks`, para não
  medir o algoritmo contra ele mesmo. As perguntas reais saem marcadas para
  conferência manual.
* **Esforço baixo por padrão**, e vale rodar um modelo menor: se o modelo
  acerta tudo nos dois braços, há efeito de teto e a medição não informa nada.

Custo estimado do piloto: **abaixo de US$ 1** para 16 sintéticas + 8 reais nos
três modelos.

### O que NÃO foi medido

Isto mede **correção do agrupamento**, não percepção. A pergunta "o modelo
entende melhor a tela com o grafo?" exige avaliação com modelo no laço: um
conjunto de perguntas sobre telas ("a que está setado o Scripting Backend?")
respondidas com e sem os grupos, com taxa de acerto comparada. Isso custa
chamadas de API e ainda não foi feito.

Também não foi medido em tela real de Windows: tudo aqui é cena sintética.

---

## Item 4 — Congelado, não descartado

Os dirty rects do `IDXGIOutputDuplication` **não vão ser implementados**, por
três razões que se somam:

1. **O flip model os esvazia onde importa.** Unity (DX11/DX12 com
   `FLIP_DISCARD`), Chrome e Electron apresentam buffers alternados: o DWM
   invalida a janela inteira a cada frame. O `AcquireNextFrame` devolve a
   viewport 3D toda na lista de dirty rects mesmo quando mudou um único
   controle. O ganho de "reprocessar só o que mudou" vai a zero justamente na
   Unity, que é o caso de uso principal.
2. **A detecção de mudança já está resolvida.** O dHash em grade 4×4 com
   supressão de células voláteis (IDEIAS/02) responde "a tela mudou?" em ~5 ms,
   sem COM, sem interoperabilidade de baixo nível e sem depender de driver.
3. **A pergunta de interatividade foi para outro lugar.** O que motivava olhar
   dirty rects era saber o que é interativo; o hover ping resolve isso com 0%
   de falso positivo a ~180 ms por elemento, sob demanda.

Vale registrar o que o argumento 3 **não** prova: dirty rects e hover ping
respondem perguntas diferentes — "o que mudou" contra "isto reage". O que
realmente sustenta o congelamento são os argumentos 1 e 2.

**Condição para descongelar:** se o SoM em janela *não acelerada* (diálogo,
Explorer, painéis do editor) virar o gargalo do agente, o SoM incremental por
dirty rect volta a valer. Enquanto o gargalo for a inferência e não a captura,
não vale.

**Substituído.** O objetivo do item 4 — não reenviar o que não mudou — é
atingível sem DXGI nenhum, por diff das próprias marcas. Proposta abaixo.

---

## Item 4' — Snapshot incremental por tags estáveis — **ENTREGUE E MEDIDO**

Implementado em três commits. Abaixo a medição; a proposta original segue logo
depois, para registro do que foi planejado contra o que saiu.

### Economia medida (60 marcas, 12 rodadas por cenário)

| Cenário | Sem diff | Com diff | Economia | full/diff |
|---|---:|---:|---:|---:|
| Tela parada | 2022 tok | 185 tok | **90,8%** | 1/11 |
| Um campo editado | 2022 tok | 214 tok | **89,4%** | 1/11 |
| Painel trocado (12 de 60) | 2022 tok | 555 tok | **72,5%** | 1/11 |
| Jitter de OCR ±2 px | 2022 tok | 185 tok | **90,8%** | 1/11 |
| Tela nova a cada chamada | 2022 tok | 2022 tok | **0,0%** | 12/0 |

O `1/11` é o completo periódico entrando uma vez a cada dez diffs, por desenho.

### Escala — a economia melhora com o tamanho da tela

| Marcas | Sem diff | Com diff | Economia |
|---:|---:|---:|---:|
| 10 | 330 tok | 71 tok | 78,4% |
| 60 | 2022 tok | 214 tok | 89,4% |
| 240 | 8246 tok | 733 tok | **91,1%** |

O custo fixo do envelope do diff dilui conforme a tela cresce. É o inverso do
grafo, cuja sobrecarga era pior justamente nas telas pequenas.

### Custo de CPU

| Marcas | Tempo do diff |
|---:|---:|
| 60 | 0,086 ms |
| 240 | 0,291 ms |
| 1000 | 1,279 ms |

Desprezível perto dos 300–1000 ms do OCR. **O diff não reduz o custo de
capturar — só o de transmitir.** Se o gargalo for a captura, isto não ajuda.

### Quando o diff fica maior que o completo

A guarda de 80% existe porque o caso ruim é real:

| Marcas trocadas | Resposta | |
|---:|---|---|
| 10% | diff | economia 88% |
| 50% | diff | economia 45% |
| 75% | diff | economia 20% |
| 100% | **full** | `diff_nao_compensa` |

Tela que muda inteira gera `removed` de N tags **mais** `added` de N marcas
cheias — maior que simplesmente mandar as marcas.

Vale notar que as duas guardas cobrem coisas diferentes e não se substituem:
`diff_nao_compensa` pega troca de *elementos* (tags novas), e
`identidade_instavel` pega troca de *conteúdo sob a mesma tag*. Uma tela que
troca 50% dos elementos passa pela segunda e é barrada só pela primeira.

### Ressalva da medição

O cenário de jitter aplica ±2 px sobre a posição da rodada anterior, então o
delta entre leituras consecutivas fica dentro da tolerância — o caso benigno,
não o pior caso.

### Medido em Windows real — o jitter é menor do que eu supunha

WinRT OCR, Unity 6.5, 10 capturas de tela parada, 385 pares de palavras:

| Deslocamento do centro | Ocorrências | Acumulado |
|---|---|---|
| exatamente 0 px | 383 | 99,48 % |
| ≤ 1 px | 385 | **100 %** |
| > 1 px | 0 | — |

Médio 0,003 px, máximo 0,5 px de centro e 1,0 px de borda (arredondamento do
rasterizador). A ressalva estava errada na direção conservadora: o WinRT não
oscila ±2 px em torno da posição verdadeira, ele repete a mesma coordenada.

`SNAPSHOT_MOVE_TOLERANCE` foi para **3** — 2 px de folga sobre o pior caso
medido, ainda pegando arraste e scroll real (≥ 4 px). O valor antigo (2)
também não produzia falso positivo nesses dados; 3 é margem, não correção.

### O ruído real não é de coordenada, é de segmentação

O que de fato oscila é o **reconhecimento**: palavra de fonte pequena
desaparece e volta entre capturas. Isso vira `removed` + `added` no diff, e
tolerância de bbox não ajuda — a marca não se moveu, ela sumiu.

---

## Item 6 — Carência de flicker — **ENTREGUE E MEDIDO**

### Duas coisas que não são a mesma

Um elemento que pisca coloca duas perguntas separadas, e tratá-las como uma só
é o erro disponível:

1. **A tag ainda é a mesma?** Sim — e isso já era verdade antes deste item.
2. **Dá para clicar nela agora?** Não. Ela não está na tela.

Confundir as duas produz o pior resultado possível: `click_tag` acertando o
último lugar conhecido de um elemento que sumiu, e reportando `ok: true`.

### O que já existia — correção à minha própria leitura

`assign_stable_som_tags` tem `SOM_TRACK_MAX_MISSES = 30` e **nunca recicla
tags**. Medido contra o rastreador real:

| Sumiu por | Tag ao voltar |
|---|---|
| 1, 2, 3, 5, 10 capturas | **a mesma** |
| 31 capturas | outra (track expirou) |

Então eu estava errado ao dizer, no relatório anterior, que "a tag some de
baixo do agente". A identidade sobrevive. O que quebra é o **diff**, que
compara só contra a última lista e anuncia `removed` de algo que o rastreador
não perdeu. A carência não cria identidade — ela impede o diff de negar a que
já existe.

### Escolhendo N por medição

O dado real disponível sobre o flicker é qualitativo ("fontes pequenas somem e
voltam"), sem a distribuição dos intervalos. Então o que se varre é a **taxa de
falha por captura**, p, e lê-se o resultado ao longo de todo o intervalo
plausível. 60 marcas, 20 % frágeis, 200 capturas:

| p(falha) | N=0 | N=1 | N=2 | N=3 |
|---|---|---|---|---|
| 0,05 | 209 | 8 (96,2 %) | 0 (**100 %**) | 0 (100 %) |
| 0,10 | 413 | 55 (86,7 %) | 7 (**98,3 %**) | 3 (99,3 %) |
| 0,20 | 707 | 160 (77,4 %) | 29 (**95,9 %**) | 13 (98,2 %) |
| 0,30 | 930 | 276 (70,3 %) | 76 (**91,8 %**) | 33 (96,5 %) |

*(eventos `removed` + `added` num diff que deveria estar vazio; entre
parênteses, o churn eliminado)*

Ganho marginal de cada captura a mais:

| p | 0→1 | 1→2 | 2→3 |
|---|---|---|---|
| 0,05 | +96,2 pp | +3,8 pp | +0,0 pp |
| 0,10 | +86,7 pp | +11,6 pp | +1,0 pp |
| 0,20 | +77,4 pp | +18,5 pp | +2,3 pp |
| 0,30 | +70,3 pp | +21,5 pp | +4,6 pp |

**N = 2.** O joelho é nítido e não depende de p: a segunda captura ainda vale
entre 4 e 21 pontos, a terceira nunca passa de 4,6 e some para p baixo. Cada
captura de carência é uma captura a mais em que a marca fica invisível para o
agente, então a terceira é custo sem retorno. `SNAPSHOT_QUARANTINE_CAPTURES=0`
restaura o comportamento antigo.

### O risco que o usuário levantou: impostor na posição

Um elemento *diferente* pode nascer onde estava a marca em carência e herdar a
tag do rastreador. O resgate reusa `_identidade_quebrada` — a mesma regra já
auditada para "a tag é a mesma mas o elemento é outro". Medido com o pior caso
injetado (mesma tag, mesma posição), em N = 1, 2 e 3:

| Impostor | Veredito |
|---|---|
| mesmo `type`, texto sem parentesco | **rejeitado** (`removed` + `added`) |
| `type` `text` → `icon` | **rejeitado** |
| mesmo texto, 200 px de distância | **rejeitado** |
| `"Campo 7"` → `"Campo 7 Extra"` | aceito como o mesmo |

O quarto é aceito **de propósito**: texto com parentesco de substring é
exatamente o caso que o rastreador já trata como o mesmo controle (OCR relendo
o mesmo rótulo). N não muda nenhum veredito — quem decide é a regra de
identidade, não a carência.

### Identidade não é permissão de clique

Marca em carência **não entra em `marks`** e não é clicável. Ela aparece em
`quarantined` (no diff e no completo), para o agente ver que a tag existe e não
está disponível agora, em vez de descobrir isso com um clique no vazio.

`click_tag` numa tag em carência **recaptura e valida** antes de qualquer coisa:

| Situação | Resposta |
|---|---|
| reapareceu compatível | clica, com `revalidated: true` |
| ainda ausente | `target_not_currently_visible`, `reason: ainda_ausente` |
| voltou outro elemento | `target_not_currently_visible`, `reason: identidade_incompativel` |

HTTP 409, com `last_known` para o agente saber onde estava. Em nenhum caso se
clica no último lugar conhecido.

### O traço real — medições em Windows real (fontes normais e fontes pequenas)

Duas baterias empíricas foram executadas na máquina real contra o Unity Editor:

1. **Fontes normais (UI padrão):** 30 capturas consecutivas, 110 entidades textuais, 3.300 amostras. 0 eventos de flicker ($p < 0,0009$).
2. **Fontes pequenas de propósito (Console denso + Inspector com $h \le 13\text{ px}$):** 40 capturas consecutivas, 86 palavras totais, 52 de fonte pequena ($h \le 13\text{ px}$), 3.440 amostras observadas.

| Conjunto testado | Entidades | Flicker observado ($k \ge 1$) | Gaps | Impostores | Churn no diff |
|---|---|---|---|---|---|
| **Fontes normais de UI** | 110 | **0 (0,00 %)** | **0** | **0** | **0** |
| **Fontes pequenas densas** ($h \le 13\text{ px}$) | 52 | **0 (0,00 %)** | **0** | **0** | **0** |

Não foi observado flicker de segmentação nas 40 capturas deste ambiente, inclusive nas 52 entidades de fontes pequenas ($h \le 13\text{ px}$). Isso evita generalizar uma medição de uma máquina para todo o WinRT/Windows: o que os dados sustentam com rigor empírico é a ausência de flicker mensurável neste setup sob tela estática ($p < 0,0009$ no IC 95%).

### Decisão final sobre a carência

Seguindo a diretriz prática orientada a dados:
1. **Desativada por padrão (`SNAPSHOT_QUARANTINE_CAPTURES = 0`):** com flicker nulo no mundo real, manter carência ativa por padrão apenas prolongaria o tempo de retenção de tags obsoletas sem trazer benefício observável.
2. **Opção defensiva preservada:** o mecanismo permanece implementado e disponível de forma opt-in via flag CLI `--quarantine-captures N` ou variável de ambiente `SNAPSHOT_QUARANTINE_CAPTURES=2` caso um aplicativo específico exiba instabilidade patológica.

**Item 6 fechado.** Não avançar para `IDXGIOutputDuplication` sem necessidade concreta comprovada.

---

## Item 5 — `scope="active_dialog"` para modais — **ENTREGUE**

### O problema

`ambiguous_target` é um beco sem saída. Quando o diálogo "Salvar alterações?"
abre sobre o Unity, o texto "Salvar" existe duas vezes: no botão do modal e na
barra de trás. `click_text` acha dois candidatos, recusa escolher (certo) — e o
agente não tem nenhum parâmetro novo para tentar. Ele reformula o texto, tenta
`exact`, tenta de novo, e cada tentativa custa uma varredura SoM inteira.

O `region` já existia, mas exige que o agente **saiba** as coordenadas do modal.
Ele não sabe: o modal é justamente o que acabou de aparecer.

### Como o modal é encontrado

Não por heurística. Um diálogo modal não é "uma janela pequena e centralizada" —
essa definição erra em toolbar flutuante, em splash e em modal maximizado.

O Win32 responde direto: `GetWindow(hwnd, GW_ENABLEDPOPUP)` devolve o popup
**habilitado** que a janela possui, que é a definição operacional de modal ativo
(a janela de trás está desabilitada justamente por causa dele). Uma chamada, sem
palpite geométrico.

### A decisão que importa: escopo pedido e não encontrado **aborta**

A tentação é fazer `scope` "melhor esforço": se não houver modal, busca na tela
inteira. Isso é o pior comportamento possível. O agente pediu escopo porque quer
clicar **dentro** do modal; se o modal fechou entre a captura e o clique, cair na
tela inteira clica no botão de trás — silenciosamente, e reportando `ok: true`.

Então: escopo pedido e não resolvido devolve `scope_unavailable` com
`scope_reason` (`sem_dialogo_ativo`, `sem_janela`, `escopo_desconhecido`), HTTP
409, sem clicar em nada. `find_text_candidates` devolve lista vazia no mesmo
caso, para que nenhum chamador futuro herde o alargamento por acidente.

### Fechando o ciclo

`ambiguous_target` passou a trazer `hint` com os escopos disponíveis. O agente
que bate no erro recebe, no mesmo corpo, o parâmetro que o resolve — em vez de
ter que adivinhar que ele existe.

### Escopos aceitos

| `scope` | Região |
|---|---|
| ausente, `all`, `screen`, `tela` | tela inteira (comportamento atual) |
| `active_dialog`, `dialog`, `modal`, `dialogo` | popup habilitado da janela em foco |
| `window`, `janela` | retângulo da janela em foco |
| qualquer outro | `scope_unavailable` / `escopo_desconhecido` |

`region` explícito tem precedência sobre `scope` — não são somados.

Vale em `click_text` (cache e OCR ao vivo), em `type_into` (repassa ao
`click_text`) e na verificação pós-clique.

### Validado em Windows real — com uma correção

Testado contra o Unity 6.5 em execução (PID 13300, janela principal 198060).

**A suposição de que o Unity desenha diálogos dentro da própria janela está
reprovada.** Build Profiles, Project Settings e Preferences são janelas Win32
de verdade, classe `UnityContainerWndClass`, criadas com `Owner` = janela
principal. `GetWindow(198060, GW_ENABLEDPOPUP)` devolve o HWND do diálogo
direto, sem enumerar nada. Precisão de 100 % nos diálogos testados.

**O menu de contexto é o caso que o `GW_ENABLEDPOPUP` não pega.** Menu de botão
direito e dropdown são janelas da classe nativa do Windows `#32768`, criadas
com **`Owner` = 0**. Não pertencem a ninguém; o vínculo com a janela que os
abriu é a **thread**. `GW_ENABLEDPOPUP` devolve 0 para eles.

Então são dois mecanismos distintos, não um:

| O que está aberto | Como se acha |
|---|---|
| diálogo / janela de configuração | `GetWindow(hwnd, GW_ENABLEDPOPUP)` |
| menu de contexto / dropdown | `EnumThreadWindows(tid)` filtrando classe `#32768` |

Daí `scope="context_menu"` e `scope="active_modal"`. O composto tenta o menu
primeiro — quando os dois estão abertos, o menu fica por cima — e devolve
`scope_resolved` dizendo qual dos dois entrou, em vez de deixar o agente supor.

Submenu abre sobre o menu pai e nasce depois, então o último da enumeração é o
que vale.

### E2E em Windows real — passou

Executado contra o Unity Editor (PID 12868, HWND 67032), explorando uma
ambiguidade nativa: `"camera"` existe dentro do menu `#32768` e fora dele, nos
painéis Hierarchy e Inspector.

| Chamada | Resultado |
|---|---|
| `click_text "camera"`, sem escopo | **409 `ambiguous_target`**, 8 candidatos, recusou |
| `click_text "camera"`, `scope="context_menu"` | **200 `ok`**, `scope_resolved: "context_menu"`, clicou a tag 1299 em (270, 897) |
| idem, após `Esc` fechar o menu | **409 `scope_unavailable`**, `sem_menu_aberto`, não clicou |

O recorte saiu como `left 200, top 124, width 406, height 956` — o retângulo da
janela `#32768`, não uma heurística. Os três critérios passaram.

### Um bug que só o Windows real mostrou

`menu_de_contexto_ativo` e `janela_de_dialogo_ativa` chamavam Win32 **sem
`ensure_desktop_access()`**. Numa thread não anexada ao desktop de entrada,
`GetWindowThreadProcessId` devolvia TID 0 e o menu nunca era encontrado.
Corrigido em `01b2f29`.

A mesma falta estava em `describe_window`, num caminho bem mais quente:
`get_foreground_window_info()` passa por ele em **todo**
`publish_state_snapshot` e em **toda** `validate_frame_reference`. Thread não
anexada ⇒ `GetForegroundWindow()` = 0 ⇒ janela `None`, levando junto a
validação de frame e o `scope="window"`. Corrigido em `f748a76`.

Nenhum dos dois aparecia nos testes: o harness stuba `ensure_desktop_access`
para no-op, que é exatamente o estado "já anexado". Um teste sintético não tem
como pegar isso.

---

## Proposta original (registro do planejado)

O item 4 morreu porque os dirty rects do DWM vêm inflados para a janela inteira
em app acelerado. Mas os dirty rects eram **meio**, não fim: o fim era parar de
reenviar as 60 marcas quando 3 mudaram.

O [mcp-windows](https://github.com/sbroenne/mcp-windows) resolve isso por outro
caminho — captura a árvore inteira sempre (barato) e envia só o **diff** contra
a árvore lembrada. Mediram 95,2% de economia em formulário Electron e 13,1% em
navegação no Chrome; medições de checagem única, não benchmark.

Aqui é mais fácil que lá, por um motivo que já existe: **as tags SoM são
estáveis entre capturas**. O `assign_stable_som_tags` já mantém a identidade do
elemento por IoU + distância + texto. Sem isso não haveria diff possível — toda
marca pareceria nova a cada captura.

### Formato do `snapshotToken`

Opaco para o cliente, legível para o servidor:

```
som1.<frame_id[:12]>.<monitor>.<hwnd>.<n_marcas>.<hash_conteudo[:8]>
```

* `som1` — versão do formato; qualquer outra coisa é tratada como token velho.
* `frame_id[:12]` — liga o token ao snapshot que o produziu.
* `monitor` e `hwnd` — trocar de monitor ou de janela invalida sem comparar nada.
* `n_marcas` e `hash_conteudo` — detectam token de outro estado com o mesmo
  `frame_id` (não deveria acontecer, mas o token é entrada do cliente).

O servidor guarda **apenas o último** snapshot por `(monitor, hwnd)`. Não é
cache com histórico: token que não corresponde ao último vira resposta
completa.

### Detecção de adicionadas, removidas e alteradas

Comparação por `tag`, não por posição na lista:

| Classe | Regra |
|---|---|
| **added** | tag no novo, ausente no anterior |
| **removed** | tag no anterior, ausente no novo |
| **moved** | mesma tag, `bbox` diferente além de uma tolerância de 2 px |
| **changed** | mesma tag, mudou `text`, `bg`, `fg`, `confidence` ou `type` |
| **unchanged** | omitida do diff |

A tolerância de 2 px em `bbox` não é detalhe: sem ela, jitter de OCR faria toda
marca aparecer como `moved` e o diff seria maior que o full. É o mesmo número
que o `t8`/estressores já usam para jitter de caixa.

```json
{
  "kind": "diff",
  "baseSnapshotToken": "som1.a3f1c8...",
  "snapshotToken": "som1.9e21bb...",
  "added":   [{"tag": 61, "type": "text", "text": "Salvar", "bbox": [...]}],
  "removed": [44, 45],
  "moved":   [{"tag": 12, "bbox": [820, 162, 96, 18]}],
  "changed": [{"tag": 7, "text": "Pausar"}],
  "unchanged": 54
}
```

`unchanged` é contagem, não lista — o cliente já tem essas marcas.

### Quando mandar diff e quando mandar completo

Regra do sbroenne, adotada com o mesmo limiar: **diff só se for seguro e menor
que 80% do payload completo.** Caso contrário, completo.

Manda completo quando:

1. não veio token, ou o token não é `som1.*`;
2. o token não corresponde ao último snapshot daquele `(monitor, hwnd)`;
3. `hwnd` ou `monitor` mudaram;
4. o snapshot anterior passou do `SNAPSHOT_TTL_SECONDS` (10 s, o mesmo do `frame_id`);
5. o diff serializado ficaria ≥ 80% do completo;
6. a captura foi truncada (`_ListaComCota.truncation`) — diff sobre coleta
   incompleta descreveria remoções que não aconteceram;
7. **`> 40% das tags mudaram de identidade**, o que sugere que o rastreador
   perdeu o fio (ver riscos).

O limiar de 80% existe porque um diff pode sair maior que o completo: tela que
muda inteira gera `removed` de 60 tags mais `added` de 60 marcas cheias.

### Expiração e invalidação

O token morre quando o snapshot que ele nomeia morre. Mesmo TTL do `frame_id`,
e pelos mesmos motivos. Token expirado **não é erro** — é resposta completa com
`kind: "full"` e o token novo. O cliente nunca precisa tratar expiração.

### Relação com `frame_id`, troca de janela e `stale_frame`

São mecanismos **ortogonais e complementares**, e misturá-los seria o erro:

* `frame_id` protege **escrita**: impede clicar numa tag de uma captura velha.
  Continua exatamente como está, com `stale_frame` / `window_changed` / 409.
* `snapshotToken` otimiza **leitura**: diz o que o cliente já tem. Nunca
  autoriza ação.

Um diff **sempre** traz o `frame_id` novo, e é ele que vale para clicar. Token
velho degrada para completo; `frame_id` velho **recusa a ação**. Um nunca deve
virar o outro: recusar leitura por token velho seria hostil, e aceitar escrita
com frame velho seria perigoso.

Se `hwnd` mudou, a regra 3 já força completo antes de qualquer comparação — a
mesma porteira do `verify_window`.

### Economia estimada

Extrapolando do que já está medido aqui (payload enxuto de ~425 tokens para 30
marcas, ~14 tokens por marca) e das classes de mudança:

| Cenário | Marcas mudadas | Payload estimado | Economia |
|---|---:|---:|---:|
| Tela parada (repetir `/state`) | 0 | ~25 tok | **~94%** |
| Um campo editado | 1–2 | ~55 tok | ~87% |
| Painel trocado | 10–15 | ~230 tok | ~46% |
| Navegação, tela nova | ~todas | full | 0% |

São **estimativas aritméticas, não medição** — a distribuição real depende de
quanto a tela muda entre chamadas no uso de verdade, que é justamente o que não
foi medido. O número do sbroenne para navegação em browser (13,1%) sugere que o
caso ruim é comum; o de formulário Electron (95,2%) sugere que o caso bom
também.

### Riscos — o principal é a tag reusada

Este é o risco que merece atenção, e é consequência direta de uma escolha já
feita aqui.

O `assign_stable_som_tags` **reusa** a tag quando acha que é o mesmo elemento
(IoU + distância + texto). O mcp-windows faz o oposto: *"um controle
substituído ganha uma referência nova; snapshots nunca redirecionam uma
referência antiga para a substituta."*

Hoje o custo de um reuso errado é limitado: o agente clica na tag 7 achando que
é o botão A quando virou B, e o `frame_id` não protege porque a captura é nova.
**Com diff, o custo cresce:** a tag 7 sai como `unchanged` e é *omitida*, então
o cliente segue com a descrição antiga de um elemento que trocou. O erro deixa
de ser de uma ação e passa a persistir no modelo que o cliente tem da tela.

Mitigações, em ordem de força:

1. **`unchanged` exige igualdade de conteúdo**, não só de tag. Se `text`, `bg`
   ou `type` mudaram, é `changed` e vai no diff — mesmo que o rastreador ache
   que é o mesmo elemento. Isso cobre o caso comum (botão que vira outro botão
   costuma mudar texto ou cor).
2. **Regra 7**: mais de 40% de troca de identidade força completo.
3. **Completo periódico**: a cada N diffs (sugestão: 10), manda completo
   independentemente. Ressincroniza barato e limita quanto tempo um erro
   sobrevive.

Riscos menores: token forjado pelo cliente (o `hash_conteudo` cobre); duas
sessões pedindo diff do mesmo `(monitor, hwnd)` (só o último snapshot é
guardado, a segunda recebe completo); e marca truncada (regra 6).

### O que isso NÃO resolve

Não reduz o custo de **capturar** — OCR e Canny rodam igual. Reduz só o custo de
**transmitir**. Se o gargalo for a captura, isto não ajuda; e a medição de
latência até hoje diz que o gargalo é a inferência, não a captura, o que é
exatamente o argumento a favor.

---

---

## Limitações Conhecidas

* **Cor de frente em texto antialiasado é aproximada** (medido: `#F3E8FF` volta
  como `#E6DBF3`). O fundo é exato; a frente, não. `bg_share` baixo sinaliza
  região heterogênea onde "cor dominante" não quer dizer grande coisa.
* **Hover tem efeito colateral.** Passar o mouse abre tooltip, menu e, em alguns
  apps, dispara navegação. O ping precisa devolver o cursor à posição anterior e
  nunca ser usado durante uma skill em replay.
* **Grafo geométrico erra em layout sobreposto.** Modal, popup e drawer quebram a
  regra de contenção — caixa contida visualmente sem ser filha lógica.
* **Dirty rects são por monitor e morrem em troca de modo.** A interface fica
  inválida em transição DWM/fullscreen (`DXGI_ERROR_ACCESS_LOST`) e precisa ser
  recriada; qualquer uso tem que ter esse caminho de recuperação.
* **Nada disso foi medido em Windows real.** Os números de token são aritmética
  verificável; o resto é projeto.

---

## Relação com as Ideias 01 e 02

A IDEIAS/01 quer valores exatos de uma **imagem parada**. A IDEIAS/02 quer
procedimento exato de uma **execução**. Esta quer descrição exata de uma **tela ao
vivo** — e as três compartilham a mesma tese:

> Quando existe um valor mensurável, **meça** — não peça para o modelo adivinhar.

A diferença desta é acrescentar um segundo verbo. Onde não há valor a medir —
"isto é clicável?" — não adianta olhar com mais atenção.

> **Pergunte.** Toque e observe o que responde.
