# Ideia 02 — Memória de Execução (skills compiladas a partir do que já deu certo)

> **Status:** Ideia
> **Data:** 2026-09-22
> **Base:** `remote_control_server.py` + `AGENTS_PROTOCOL.md`

---

## O Problema Real

O agente de IA refaz todo o raciocínio do zero em toda execução. A tarefa que ele
resolveu ontem em 12 passos, ele resolve hoje nos mesmos 12 passos, pagando de
novo o preço inteiro — inclusive na quadragésima vez.

O custo não está no clique. Está na **percepção e na chamada de modelo por passo**.
Usando os números do seu próprio `AGENTS_PROTOCOL.md`:

| Etapa de um passo | Custo |
|---|---|
| Clique Win32 `SendInput` | ~1 ms |
| Template match com `--window` | < 15 ms |
| Captura + OCR/SoM da tela inteira | 300–1000 ms |
| Ida e volta ao modelo de visão | 2–10 s |

Uma tarefa de 12 passos custa alguns segundos de execução real e **quase um minuto
de percepção e inferência** — mais tokens, mais latência, mais chance de erro. E o
agente não fica melhor: a taxa de acerto da quadragésima execução é exatamente a
da primeira.

O `AGENTS_PROTOCOL.md` já percebeu isso. A seção 1 abre proibindo screenshot
repetida em disco, e a seção 2 manda recortar o ícone à mão e salvar em
`templates/`. O diagnóstico está certo, mas **a solução é manual**: quem aprende é
você, recortando PNG, não o servidor. Não escala, e não sobrevive a você mudar de
tarefa.

---

## A Ideia

O servidor passa a **gravar o que deu certo e recompilar aquilo em um programa
replayável sem IA.**

Na primeira vez, o agente resolve a tarefa do jeito caro de sempre: SoM, OCR,
modelo de visão, `click_and_verify`. O servidor observa. Quando a tarefa termina
com a pós-condição confirmada, ele compila aquela trajetória em uma **skill**: uma
pequena máquina de estados onde cada passo carrega *uma verificação da tela* e
*uma ação*.

Na segunda vez, o agente diz `POST /skill/run {"name": "abrir_build_settings"}` e
o servidor executa os 12 passos em menos de um segundo, **sem nenhuma chamada de
modelo**, conferindo a tela antes de cada clique. Se em algum passo a tela não for
a esperada, ele **para, não chuta**, devolve o estado ao vivo e o agente assume
dali — sem perder o prefixo que já funcionou.

```
1ª execução:   [IA raciocina 12x] ──► tarefa feita ──► servidor COMPILA a skill
2ª execução:   [POST /skill/run] ──► 12 passos verificados ──► ~1s, 0 tokens
Nª execução:   idem. Se a UI mudou ──► diverge no passo K ──► IA assume do passo K
                                                          ──► servidor RECOMPILA
```

---

## Isso Já Existe? Sim — e Isso é a Melhor Notícia

Não vou vender novidade. Essa ideia foi validada por vários grupos em 2026, com
números publicados:

| Trabalho | O que faz | Resultado medido |
|---|---|---|
| [PreAct](https://arxiv.org/abs/2606.17929) | Compila a 1ª execução bem-sucedida numa máquina de estados; cada estado confere a tela, cada transição age | **8,5–13x mais rápido**, zero chamadas de modelo no replay |
| [EchoPath](https://arxiv.org/abs/2609.16635) | Memória replayável no nível da execução, com pré-condições, evidência visual e re-mira do alvo por imagem (IBTR) | **−90% de custo em tokens**, **−60% de tempo** |
| [SkillDroid](https://arxiv.org/abs/2604.14872) | Compila trajetórias em templates parametrizados com slots tipados; recompila quando a confiabilidade cai | Sucesso sobe de 87%→91% enquanto o baseline cai 80%→44%; **−49% de chamadas de modelo** |
| [Agent Workflow Memory](https://futureagi.com/glossary/agent-workflow-memory/) | Induz workflows reutilizáveis das trajetórias passadas | — |

O ponto que importa: **os três são pesquisa, rodando em Android ou em harness
genérico. Nenhum é uma ferramenta local de Windows.** E os três precisam construir
do zero justamente as primitivas que o seu `remote_control_server.py` já tem
prontas e depuradas.

A distinção que o PreAct faz é a que separa isso de uma macro de RPA: **uma macro
dispara os passos gravados às cegas; aqui cada passo confere a tela antes de agir e
devolve o controle quando algo não bate.** É por isso que dá para confiar no replay.

---

## Por Que Este Servidor é o Lugar Certo

Das cinco peças necessárias, o arquivo já tem três — e são as três mais difíceis:

| A skill precisa de… | O servidor já tem | Onde |
|---|---|---|
| Agir sem ambiguidade sobre qual elemento | Tags SoM estáveis entre capturas, que nunca reciclam ID | `assign_stable_som_tags` |
| Garantir que a tela não mudou entre ver e clicar | `frame_id` + TTL + `verify_window`, com HTTP 409 em `stale_frame` | `validate_frame_reference` |
| Confirmar que a ação teve o efeito esperado | Clique que só retorna `ok` depois de observar o resultado | `click_and_verify` |
| Re-mirar o alvo quando a janela moveu/redimensionou | Template matching restrito à janela, < 15 ms | `locate_template_on_screen` |
| **Assinatura barata do estado da tela** | **falta** | — |
| **Diário de execução + compilador + replayer** | **falta** | — |

A "re-mira por imagem" que o EchoPath chama de IBTR e trata como contribuição do
paper é, aqui, uma chamada de função que já existe. É disso que estou falando
quando digo que o custo de implementação é baixo: **o que falta é a memória, não a
mecânica.**

---

## Arquitetura — 5 Componentes

### 1. Assinatura de estado (o que falta, e é barato)

Um `dHash` 8×8 da área cliente da janela em foco (64 bits), em escala de cinza.
Custa ~5 ms — contra 300–1000 ms de uma varredura OCR completa. Comparação por
distância de Hamming com tolerância, então antialiasing e um pixel de sombra não
quebram o match.

A assinatura completa de um estado é:

```
dhash(área cliente)  +  processo  +  regex do título  +  tamanho da área cliente
```

**Células voláteis.** Um relógio, um spinner, o contador de frames da Unity mudam
sozinhos e quebrariam qualquer hash da tela inteira. Solução: ao compilar, capturar
o mesmo estado 3× ao longo de ~1 s e marcar como **volátil** toda célula da grade
4×4 que variou entre as capturas. Essas células saem da comparação. É barato e
resolve a causa número um de falso negativo.

### 2. Diário de execução

Toda ação que já retorna `ok` hoje traz o que foi feito, onde e em qual `frame_id`.
Basta anexar isso a um diário circular em memória, junto da assinatura de estado
antes e depois. Custo próximo de zero — a informação já está sendo produzida e
descartada.

### 3. Compilador

`POST /skill/compile` pega os últimos N registros do diário e emite a máquina de
estados. Para cada passo, guarda:

- a **pré-condição** (assinatura do estado onde o passo é válido),
- a **ação** (o mesmo payload JSON que o servidor já aceita hoje),
- a **evidência visual** (o recorte PNG do elemento clicado, via `get_screen_crop`),
- a **pós-condição** (assinatura do estado que deve aparecer depois, com timeout).

**Só compila se a tarefa foi verificada.** Trajetória que terminou sem pós-condição
confirmada não vira skill. Isso é o que impede o servidor de memorizar um erro.

### 4. Replayer

`POST /skill/run`. Para cada passo, na ordem: confere a pré-condição → executa →
espera a pós-condição. Qualquer divergência **interrompe** e devolve:

```json
{
  "ok": false,
  "error_code": "skill_diverged",
  "skill": "unity_abrir_build_settings",
  "failed_at_step": 7,
  "reason": "precondition_mismatch",
  "hamming_distance": 21,
  "completed_steps": 6,
  "live_state": { "...payload igual ao do /state, com marks e frame_id..." }
}
```

O agente recebe no mesmo JSON o estado ao vivo para assumir dali. **Os 6 passos que
funcionaram não são refeitos.**

### 5. Re-mira antes de desistir

Divergiu por causa da coordenada (janela moveu, foi redimensionada, mudou de
monitor)? Antes de devolver `skill_diverged`, o servidor tenta localizar a
evidência visual daquele passo dentro da janela com `locate_template_on_screen`.
Achou com confiança alta → re-mira e segue. Não achou → aí sim diverge.

Isso cobre o caso mais comum de quebra sem precisar de IA nenhuma.

---

## Formato da Skill — `skills/unity_abrir_build_settings.json`

```json
{
  "skill": "unity_abrir_build_settings",
  "version": 3,
  "app": {
    "process": "Unity.exe",
    "title_regex": "^Unity 6\\.5.*",
    "client_size": [1920, 1017]
  },
  "params": [
    { "name": "plataforma", "type": "string", "default": "Windows" }
  ],
  "compiled_from": {
    "journal_id": "a3f1c8",
    "at": "2026-09-22T14:20:00Z",
    "verified_by": "click_and_verify"
  },
  "stats": {
    "runs": 41, "ok": 39, "diverged": 2,
    "last_ok": "2026-09-22T18:02:11Z",
    "avg_ms": 840
  },
  "steps": [
    {
      "n": 1,
      "precondition": {
        "dhash": "9f1c4a70e2b8d316",
        "tolerance": 6,
        "volatile_cells": [12, 13]
      },
      "action": { "action": "click_text", "text": "File", "monitor": "1" },
      "evidence": {
        "template": "skills/unity_abrir_build_settings/s1_file.png",
        "bbox_client": [12, 34, 40, 20]
      },
      "postcondition": {
        "dhash": "3ab8f00c91d4e257",
        "tolerance": 6,
        "timeout_ms": 1500
      }
    },
    {
      "n": 2,
      "precondition": { "dhash": "3ab8f00c91d4e257", "tolerance": 6 },
      "action": { "action": "click_text", "text": "Build Settings" },
      "evidence": {
        "template": "skills/unity_abrir_build_settings/s2_build.png",
        "bbox_client": [28, 210, 120, 22]
      },
      "postcondition": {
        "text_present": "Build Settings",
        "timeout_ms": 4000
      }
    }
  ]
}
```

Parâmetro entra no payload como `"text": "{{plataforma}}"`. Sem template engine,
só substituição de string.

---

## API Nova

| Rota | O que faz |
|---|---|
| `POST /skill/compile` | Compila os últimos N passos do diário numa skill nomeada |
| `POST /skill/run` | Replaya a skill com verificação passo a passo |
| `GET /skills` | Lista skills com `stats` (taxa de acerto, tempo médio, última execução) |
| `POST /skill/forget` | Invalida uma skill que a UI quebrou de vez |
| `GET /state?signature=1` | Só a assinatura do estado, sem OCR — os tais ~5 ms |

Tudo passa pelo mesmo `execute_system_action`, então herda o lock de input, o
`verify_window` e o tratamento de erro que já existem.

---

## O Que Impede de Clicar no Lugar Errado

A disciplina toda vem de uma regra: **observar, depois agir. Nunca o contrário.**

1. Skill não roda se `process` e `title_regex` não baterem.
2. Todo passo confere a pré-condição **antes** de mandar o clique.
3. Divergência **para** a execução. Nunca continua "tentando".
4. Coordenada nua nunca é replayada sozinha — sempre tem evidência visual atrás.
5. Skill só entra no acervo se a tarefa foi verificada na compilação.
6. Degradação é sempre para o comportamento de hoje: divergiu, o agente assume com
   SoM ao vivo. **O pior caso do sistema novo é o caso normal do sistema atual.**

---

## Por Que a Tag SoM Sozinha Não Basta

Vale registrar, porque é tentador achar que as tags persistentes já resolvem: no
`.remote_control_som_tags.json` de hoje, o monitor 1 está com `next_tag: 990` e 423
tracks depois de apenas 81 gerações. As tags **derivam** — o OCR lê o mesmo
controle de forma um pouco diferente, o elemento sai e volta, e ganha tag nova.

Ou seja: tag é ótima **dentro** de uma sessão de captura, e não é confiável como
identidade **entre** sessões. É exatamente por isso que o passo da skill guarda
evidência visual e assinatura de estado, e não o número da tag.

---

## Stack Técnica

| Componente | Biblioteca | Já está no projeto? |
|---|---|---|
| dHash da área cliente | `Pillow` + `numpy` | Sim |
| Re-mira por template | `cv2` via `locate_template_on_screen` | Sim |
| Recorte da evidência | `get_screen_crop` (em PNG, não JPEG) | Sim |
| Persistência das skills | `json` + escrita atômica já existente | Sim |
| Diário circular | `collections.deque` | Nativo |

**Zero dependência nova.** É o argumento mais forte a favor de fazer isso aqui em
vez de montar um harness separado.

---

## Resultado Esperado

Os números abaixo são **alvo**, derivados do que os papers mediram em cenário
parecido — não são medição deste servidor. O que dá para afirmar com confiança é a
ordem de grandeza: trocar 300–1000 ms de OCR mais segundos de inferência por ~5 ms
de hash mais ~15 ms de verificação é uma diferença de duas ordens de grandeza, e
isso é aritmética, não estimativa.

| Métrica | Hoje | Com memória de execução |
|---|---|---|
| Tarefa repetida de 12 passos | ~45 s + 12 chamadas de modelo | < 1 s, 0 chamadas |
| Tokens numa tarefa já conhecida | integral | próximo de zero |
| Confiabilidade na Nª execução | igual à 1ª | cresce (recompila quando degrada) |
| Custo de um passo novo | igual ao de hoje | igual ao de hoje |

---

## Limitações Conhecidas

- **UI que muda de verdade** (update da Unity que reposiciona menu) invalida as
  skills daquele app. Mitigação é recompilar, não evitar.
- **Conteúdo dinâmico** dentro do estado (lista que carrega, preview que anima)
  exige que as células voláteis sejam bem marcadas. Com 3 capturas pode escapar
  alguma; provavelmente vai precisar de ajuste no número de amostras.
- **Tema claro/escuro e mudança de DPI** trocam o dHash inteiro. Na prática viram
  skills separadas, indexadas por `client_size` e tema.
- **Tarefa genuinamente nova** não ganha nada. O ganho é proporcional à repetição —
  o que casa com o seu uso real (Unity, mesmos menus, todo dia).
- **Não substitui o agente.** Substitui a parte do trabalho do agente que já foi
  feita e deu certo.

---

## Próximos Passos

- [x] MVP da assinatura: `GET /state?signature=1` com dHash da área cliente + processo + título
- [x] Marcação de células voláteis por 3 capturas sucessivas
- [x] Diário circular gravando `{assinatura_antes, ação, assinatura_depois}` em toda ação `ok`
- [x] `POST /skill/compile` com recorte de evidência em PNG
- [x] `POST /skill/run` com verificação passo a passo e retorno `skill_diverged` + estado ao vivo
- [x] Re-mira via `locate_template_on_screen` antes de declarar divergência
- [ ] Recompilação automática quando a taxa de acerto da skill cair abaixo de um limiar
- [ ] Seção nova no `AGENTS_PROTOCOL.md`: *tente `/skills` antes de olhar a tela*

---

## Status da Implementação

As cinco peças estão no `remote_control_server.py`:

| Entregue | Onde |
|---|---|
| **1. Assinatura** — `compute_state_signature()`, grade 4×4, células voláteis, `signature_distance()` | `GET /state?signature=1`, `POST /signature`, `--signature` |
| **2. Diário** — grava assinatura antes / ação / assinatura depois / recorte PNG do alvo | `POST /journal`, `/journal/start`, `/journal/stop`, `/journal/clear` |
| **3. Compilador** — trecho do diário vira máquina de estados persistida | `POST /skill/compile`, `/skill/forget`, `GET /skills` |
| **4. Replayer** — confere a tela antes de cada passo; divergiu, para e devolve o controle | `POST /skill/run` |
| **5. Re-mira (IBTR)** — reencontra o alvo pela evidência visual antes de desistir | dentro do `/skill/run`, desligável com `reaim: false` |

### Decisões que mudaram em relação ao projeto original

**A porteira do app é o processo, não o título.** Travar no título exato tornaria
a skill inútil: título de app real carrega nome de cena, arquivo aberto e
asterisco de não-salvo. O título fica gravado como `title_sample`, e quem quiser
preenche `title_regex` à mão.

**As células voláteis saem de graça do próprio diário.** O `after` do passo N e o
`before` do passo N+1 são duas capturas do *mesmo* estado separadas por tempo
real — o que difere entre elas é relógio e spinner. Não precisa de captura extra
na compilação. Só que isso sozinho deixa uma skill de **um passo** sem proteção
nenhuma (não há par), então a captura `before` do diário tira 2 amostras.

**A re-mira não dispensa a pós-condição.** Ela age sobre uma tela que *já
reprovou* na pré-condição, então exige confiança alta (0,9) e o resultado
continua sendo julgado pela pós-condição. Se o alvo não for encontrado, diverge
sem clicar.

**O `click_offset` importa.** O centro do template não é o ponto clicado: o
recorte é feito em volta do clique, então o offset gravado é o que traduz "achei
o elemento aqui" em "o clique vai neste pixel".

### Verificado

47 checks em 6 suítes (Win32 stubado, Pillow real, telas sintéticas): ruído de
antialiasing move 1 bit e layout diferente move 28; pipeline grava 1 entrada e
não 3; ação que falhou nunca vira matéria-prima; nome de skill não faz path
traversal; tela errada diverge **sem clicar**; travar no passo 3 preserva os 2
anteriores e `from_step` retoma dali; outro processo em foco barra na porteira;
re-mira reposiciona `[150,120] → [400,300]` e, sem achar o alvo, diverge sem
clicar. Um replay de 3 passos roda em **13 ms**, com zero chamadas de modelo.

Falta: recompilação automática quando a taxa de acerto cair, e a seção no
`AGENTS_PROTOCOL.md`.

---

## Relação com a Ideia 01

O `01` extrai tokens de **imagem parada** para a IA gerar um site. O `02` extrai
procedimento de **execução real** para a IA parar de repetir raciocínio. São eixos
diferentes, mas compartilham a mesma tese, que é a tese boa do `01`:

> Quando existe um valor mensurável, **meça** — não peça para o modelo adivinhar.

O `01` aplica isso a cor e espaçamento. O `02` aplica a caminho e estado.
