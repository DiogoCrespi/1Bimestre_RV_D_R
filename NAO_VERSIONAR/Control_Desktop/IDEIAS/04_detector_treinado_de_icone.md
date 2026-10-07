# Ideia 04 — Detector Treinado de Ícone: onde o OmniParser entra, e onde não entra

> **Status:** OmniParser **não integrado** (decisão mantida). A medição que este
> documento propunha foi feita, e o caminho clássico virou o detector v2 — ver
> "Resultado medido" no fim.
> **Data:** 2026-09-26 (proposta) · 2026-09-27 (resultado)
> **Base:** `inspect_screen_som()` e o rastreador de tags em `remote_control_server.py`;
> IDEIAS/03 (percepção) e IDEIAS/02 (memória de execução)
> **Origem:** análise de viabilidade de um pipeline de agente de tela baseado em
> detecção de objetos (caixas coloridas sobre elementos clicáveis)

---

## O pedido, e o que ele revela

A proposta analisada descreve um agente de tela em quatro etapas:

```
[Captura] → [Detecção de objetos (boxes/cores)] → [Raciocínio (LLM)] → [Execução (mouse/teclado)]
```

Com ferramentas sugeridas: `mss`/OpenCV para capturar, **OmniParser ou YOLOv8**
para detectar, GPT-4o/Claude para decidir, **PyAutoGUI/pynput** para executar.

O primeiro resultado da análise é desconfortável e útil: **três das quatro
etapas já existem aqui**, e em duas delas a proposta é pior do que o que
rodamos hoje.

| Etapa | Proposta | Nosso estado |
|---|---|---|
| 1. Captura | `mss` / OpenCV | dxcam + GDI, com oráculo DXGI (watermark, settle, dirty rects) |
| 2. Boxes + ID | OmniParser / YOLOv8 | SoM: WinOCR + auto-Canny, `type`, `bg`/`fg`/`contrast`, **tag estável** |
| 3. Raciocínio | LLM na nuvem | idem, com `/state`, escopo, snapshot incremental |
| 4. Execução | PyAutoGUI / pynput | Win32 `SendInput` + desktop de entrada |

O que sobra de genuinamente novo é a **etapa 2 com modelo aprendido**. É só
disso que este documento trata.

---

## Duas coisas da proposta que seriam regressão

Registradas porque são tentadoras e porque o custo delas já foi pago aqui.

### PyAutoGUI no lugar do `SendInput`

Saímos do PyAutoGUI de propósito. Ele não alcança janela elevada, sofre com
virtualização de DPI, e não faz `OpenInputDesktop` / `SetThreadDesktop`.

Esse último ponto não é teórico: em setembro de 2026, dois bugs em Windows real
foram exatamente *falta de anexo ao desktop de entrada* — `01b2f29` (o menu de
contexto nunca era encontrado, porque `GetWindowThreadProcessId` devolvia TID 0)
e `f748a76` (`describe_window` devolvendo janela `None`, levando junto a
validação de frame). Uma camada que não expõe esse controle não tem como
consertar essa classe de falha.

### Renumerar IDs a cada quadro

O pipeline descrito re-detecta e re-numera tudo a cada captura. Aqui a tag
sobrevive **30 capturas** (`SOM_TRACK_MAX_MISSES`) e **nunca é reciclada** —
medido contra o rastreador real: some por 1, 2, 3, 5 ou 10 capturas e volta com
a mesma tag.

Três coisas dependem disso:

- **snapshot incremental** — o diff é por tag, não por posição na lista (90,8 %
  de economia medida em tela parada);
- **carência de flicker** — segurar identidade por N capturas só faz sentido se
  a identidade existir;
- **`click_tag`** — um plano de vários passos referencia tags entre capturas.

Adotar IDs por quadro custaria as três. Se um detector aprendido entrar, as
caixas dele têm que passar por `assign_stable_som_tags`, e não trazer índice
próprio.

---

## O que o detector aprendido resolveria: a nossa fraqueza medida

Hoje o ícone sai de auto-Canny + `MORPH_CLOSE` + `connectedComponentsWithStats`,
com quatro heurísticas (densidade, aspecto, desvio-padrão, tamanho) viradas em
`confidence = round(score / 4.0, 2)` — **cinco valores possíveis**: 0, 0,25,
0,5, 0,75 e 1,0. Com `SOM_ICON_MIN_CONFIDENCE = 0.85`, na prática exige as
quatro heurísticas.

Duas evidências de que isso é frágil, ambas vindas do nosso próprio histórico:

1. **O hover ping existe por causa disso.** Ele foi construído só para
   desempatar a faixa intermediária de confiança, e a quantização em cinco
   degraus chegou a produzir uma janela de confiança de interseção vazia
   (`HOVER_CONF_MIN` acima do valor que o detector conseguia emitir).
2. **`icon_candidate`.** Precisamos de um segundo tipo de marca só para os
   ícones que o detector encontra mas não consegue afirmar.

E o detector só diz **onde** tem algo clicável. Nunca **o que é**.

### No Unity isso dói mais do que em qualquer outro app

- A toolbar é quase toda ícone: Play / Pause / Step, hand / move / rotate /
  scale / rect, toggles de gizmo.
- A UIA é praticamente inútil — é tudo desenhado na mão, não são controles Win32.
- O WinOCR não lê ícone.

Ou seja: no app que é o caso de uso principal deste servidor, a região mais
importante da tela é justamente a que a nossa percepção enxerga pior.

### O que o OmniParser V2 é, concretamente

Um par de modelos: **YOLOv8 afinado** para localizar região interativa, mais
**Florence-2** para descrever a função do ícone.

O número relevante para nós é o **ScreenSpot Pro**, benchmark feito de tela
grande com alvo pequeno — a descrição da toolbar do Unity:

| | Acurácia média |
|---|---|
| GPT-4o sozinho | 0,8 |
| GPT-4o + OmniParser V2 | **39,6** |

O ganho relativo é enorme. E 39,6 em absoluto continua baixo: **não é problema
resolvido**, é problema deslocado.

---

## O custo que decide a questão: a GPU

A recomendação usual é rodar o detector localmente na placa para a marcação ser
instantânea. Aqui isso colide com o caso de uso.

**A placa está ocupada.** Na nossa máquina ela renderiza o Unity Editor mais o
Game View. As latências citadas — ~0,6 s/quadro numa A100, ~0,8 s numa 4090 —
são de GPU **livre**. Disputando VRAM com o Unity, pior. Em CPU, segundos por
quadro, o que inviabiliza o laço do agente.

Nosso SoM hoje custa 300–1000 ms. Então a troca não é "mais rápido":

> **percepção melhor pelo mesmo preço ou mais caro.**

Some a isso PyTorch mais pesos de modelo num projeto que é deliberadamente um
arquivo só. Como **substituição** do pipeline, a conta não fecha.

---

## Proposta: fonte opcional de marcas, em processo separado

Não reescrever nada. Adicionar o detector como mais uma fonte de marcas, do
mesmo jeito que o oráculo DXGI entrou — opt-in, desligado por padrão.

**Contrato.** O detector entrega uma lista no schema que já existe:

```python
{"bbox": [x, y, w, h], "center": [cx, cy], "type": "icon",
 "text": "<descrição funcional do Florence-2>", "confidence": 0.0-1.0}
```

**Regras que não se negociam:**

1. **Passa por `assign_stable_som_tags`.** O índice do detector é descartado; a
   identidade continua sendo nossa.
2. **Processo separado.** Servidor não importa PyTorch. Comunicação por
   socket/IPC, com timeout; detector morto ou lento degrada para o detector
   clássico, nunca derruba o `/state`.
3. **Funde, não substitui.** Texto continua vindo do WinOCR, que é barato,
   determinístico e — medido — tem flicker de segmentação ≈ 0 em fontes de
   9–14 pt. O detector aprendido entra só onde o clássico é fraco: ícone.
4. **Opt-in por flag**, como `ENABLE_DXGI_ORACLE` e
   `SNAPSHOT_QUARANTINE_CAPTURES`.

Assim escopo (`active_dialog`, `context_menu`), snapshot incremental, carência,
`click_tag`, verificação de desfecho e diário continuam valendo sem tocar em
nada.

---

## Antes de qualquer linha: medir o tamanho do prêmio

Esta é a parte que o documento existe para defender.

**A pergunta**, e ela é barata de responder: *dos elementos realmente
interativos numa tela do Unity, que fração o nosso detector clássico já marca?*

**Protocolo:**

1. ~20 capturas reais da máquina, cobrindo toolbar, Inspector, Hierarchy,
   Console e pelo menos um diálogo modal.
2. Ground truth: anotar à mão o que é de fato clicável em cada uma.
3. Rodar o `inspect_screen_som()` atual sobre as mesmas imagens.
4. Reportar cobertura (recall), precisão e falso positivo, **separando texto de
   ícone** — porque a média esconde exatamente o que interessa.

**A decisão, fixada antes de ver o número:**

| Cobertura de ícone hoje | Leitura |
|---|---|
| ≥ 85 % | o detector aprendido não paga a GPU; investir em outra coisa |
| 60–85 % | vale como opt-in para quem tem GPU sobrando; não como padrão |
| < 60 % | vale mesmo com o custo; a toolbar do Unity está fora do alcance hoje |

O harness roda em container; as capturas precisam vir da máquina Windows.

### Por que fixar o critério antes

Porque já aconteceu aqui de eu ajustar a expectativa ao dado. Na IDEIAS/03, o
grafo geométrico foi avaliado com modelo no laço, deu **100 % com e 100 % sem** —
efeito de teto, avaliação fácil demais — e a conclusão correta foi deixá-lo
**desligado por padrão**, apesar de estar implementado e funcionando. E o
custo dele em tokens (+72 %/+90 %) eu cheguei a chamar de economia antes de
medir direito.

Um detector com GPU merece o mesmo ceticismo, com o critério escrito antes.

---

## O que este documento não resolve

- **Não mede nada.** Toda a seção de custo vem de números publicados sobre
  hardware que não é o nosso, e a parte de cobertura é um protocolo, não um
  resultado.
- **Não avalia YOLOv8 treinado por nós.** Afinar detector próprio no Unity é
  outra ideia, com outro custo (dataset anotado), e não foi analisada aqui.
- **Não considera Florence-2 sozinho**, sem o YOLO, aplicado apenas aos
  `icon_candidate` que o clássico já isolou. Isso seria bem mais barato — roda
  em poucos recortes por captura em vez da tela inteira — e talvez capture a
  maior parte do ganho. Se a medição de cobertura mostrar que o problema é
  *nomear* e não *achar*, este é o caminho a explorar primeiro.

---

## Resultado medido (2026-09-27, Windows real, Unity 6.5)

Feito em rodadas com revisão externa (ChatGPT) até consenso, com a regra de
ouro combinada: calibrar só em telas de calibração, congelar o gabarito do
holdout **antes** de rodar o detector, reportar o número estrito.

### A medição proposta acima

Primeira tela do Unity, 55 controles só de ícone anotados à mão: o detector
clássico (v1) achava **25%**. Pelo critério fixado antes (< 60%), apontava para
o detector treinado. Mas a autópsia mostrou que **não era falta de
aprendizado**: 24 dos 32 perdidos estavam grudados nas bordas dos painéis (o
editor inteiro virava um componente de 1360×668) e 8 eram o menu "⋮", fino
demais para o filtro de tamanho.

### O detector v2 (clássico, sem GPU)

- remove retas ≥ 40 px antes de separar componentes;
- reconhece o "⋮" pela forma (três pontos alinhados), não pelo tamanho;
- não procura ícone dentro de menu nativo `#32768` (menu Win32 é texto);
- trata miniatura + rótulo abaixo, em grade, como um elemento só;
- deduplica com prioridade UIA > texto > ícone;
- ícone sai compacto no `/state` (225 → 99 bytes por marca).

### Números — 12 holdouts congelados, 614 pontos

| | v1 | **v2** |
|---|---|---|
| recall de ícone | 28,0% | **69,7%** |
| precisão | 61,8% | **82,5%** |
| lixo | 15,5% | 17,5% |
| duplicatas | 78 | **0** |
| bytes do `/state` | — | −4,6% |
| tempo | — | +18 ms/tela |

**As metas originais não foram atingidas** (precisão ≥ 90%, lixo ≤ 10%).
82,5% / 17,5% é o **teto observado desta família de heurísticas**. O lixo
restante (pedaço de faixa de abas, fragmento de campo de busca, polegar de
rolagem com a mesma assinatura do slider, letra X/Y/Z que o WinOCR não lê
isolada, moldura de botão) foi testado na calibração e não tem assinatura
segura. Por acordo, a iteração heurística parou aqui.

### Decisão

- **v2 é o padrão só no Unity** (`SOM_ICON_DETECTOR=auto`): ele domina o v1 nos
  quatro holdouts independentes, inclusive em precisão — manter o v1 seria
  manter o detector que mais aponta alvo falso. Fora do Unity o v2 não foi
  medido e não vale lá.
- **OmniParser segue fora**: com o clássico em 69,7% de recall, a faixa de
  decisão passou de "vale mesmo com custo" para "opcional para quem tem GPU
  sobrando". A GPU desta máquina é uma RTX 2060 de 6 GB, com 2,4 GB já em uso
  com o Unity aberto. Se um dia voltar, o alvo é o lixo restante (precisão),
  não o recall.

---

## Referências

- [OmniParser V2 — Microsoft Research](https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/)
- [microsoft/OmniParser — GitHub](https://github.com/microsoft/OmniParser)
- [microsoft/OmniParser-v2.0 — Hugging Face](https://huggingface.co/microsoft/OmniParser-v2.0)
