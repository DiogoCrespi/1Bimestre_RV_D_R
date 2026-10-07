# Ideia 05 — Menu de Ações Resolvidas: parar de deixar o agente compor

> **Status:** Ideia — um item já entregue, dois pendentes, um problema sem conserto pronto
> **Data:** 2026-09-27
> **Base:** análise do [socai-io/jev-social](https://github.com/socai-io/jev-social)
> (`f622e08`) contra `execute_system_action()` em `remote_control_server.py`
> **Já entregue daqui:** `repeated_failed_action` (commit `0922342`)

---

## De onde veio

O `jev-social` é um agente Node de pesquisa social: o modelo escolhe a próxima
operação e um CLI local executa no Chrome real. Domínio totalmente distante do
nosso — parsing de URL do Instagram, argumentos de CLI por plataforma. **Zero
linhas aproveitáveis.**

O que vale é o desenho do laço, resumido numa frase do README:

> *"Jev does not choose arbitrary DOM coordinates or generate shell commands."*

Três ideias saíram daí. A segunda foi implementada no mesmo dia; as outras duas
ficam aqui.

---

## Item 1 — Menu de operações resolvidas, em vez de verbos + alvos

### A diferença

`availableActions()` devolve uma lista em que **cada item é um comando pronto,
já validado, com alvo que apareceu num resultado anterior**. O modelo escolhe um
`id`. Não compõe nada.

Nós fazemos o oposto: entregamos a lista de marcas do `/state` e um conjunto de
verbos (`click_tag`, `click_text`, `type_into`, `click`), e o modelo combina os
dois.

**A composição é onde moram todas as nossas falhas.** Olhando a lista de códigos
de erro que fomos obrigados a criar:

| Erro | É um erro de… |
|---|---|
| `ambiguous_target` | composição: o texto casa com dois alvos |
| `target_not_currently_visible` | composição: a tag existe mas não está clicável |
| `scope_unavailable` | composição: escopo pedido que não existe agora |
| `stale_frame` / `window_changed` | composição: alvo de uma captura anterior |

Cada um deles é uma combinação que poderíamos simplesmente **não ter
oferecido**. O `jev-social` valida no momento de montar o menu
(`buildActionArgs` roda dentro do `add()`); o que não valida, não entra na lista.

### Como seria aqui

Um endpoint `/actions` que, a partir do snapshot corrente, devolve as operações
executáveis:

```json
{ "id": "click_tag_a3f19c2b41d07e58",
  "label": "Clicar em \"Build Settings\" (tag 7, barra superior)",
  "kind": "click_tag", "tag": 7 }
```

Cada item já filtrado por: tag visível (não em quarentena), texto não ambíguo,
dentro do escopo ativo, `frame_id` corrente.

### Por que não implementar já

**Custo de token, e ele é real.** 60 marcas × vários verbos é um menu grande.
Construímos o snapshot incremental justamente porque payload importa — medimos
90,8 % de economia em tela parada. Trocar isso por um menu enumerado a cada
passo desfaz parte do ganho.

**Perderíamos o clique por coordenada arbitrária**, que no Unity não é
detalhe: arrastar na Scene view, ajustar um gizmo, girar a câmera. Nada disso é
enumerável a partir de marcas.

Então: **endpoint adicional, nunca substituto**, e só depois de medir o custo do
menu contra a redução de erro de composição. A métrica honesta é *erros de
composição por tarefa concluída*, com e sem `/actions` — não "o menu parece
mais seguro".

---

## Item 2 — `repeated_failed_action` — **ENTREGUE** (`0922342`)

Registrado aqui porque a ideia veio da mesma leitura.

### O que o jev-social faz

```js
action.id = `${kind}_${sha256(JSON.stringify(args)).slice(0,16)}`;
if (!attempted.has(action.id)) actions.push(action);
```

A identidade da ação **é a sua forma executável**. E ação já tentada **sai do
menu** — o agente fica estruturalmente impedido de repetir, sem depender do
juízo do modelo.

### O que nós tínhamos

Nada. Cada chamada aqui é independente; `verification_timeout` olha **uma**
chamada. Um agente podia reenviar o mesmo clique num botão morto cinquenta
vezes sem que nada percebesse.

### A regra adotada, e por que é mais estreita que a deles

Não dá para simplesmente proibir repetição, porque aqui repetir é às vezes
correto:

- repetir com a **tela diferente** é legítimo — o botão mudou de estado;
- repetir uma ação que **deu certo** é legítimo — clicar "+" três vezes.

Só um par não tem como dar outro resultado:

> **mesma ação + mesma tela + já falhou antes**

Identidade da ação: `sha1` do corpo normalizado, sem `frame_id`, `since` nem
blobs. Comparação de tela: por células da assinatura, **ignorando as voláteis**
identificadas na captura da falha — sem isso o contador de frame da Unity
liberaria toda repetição.

### O custo fica no caminho patológico

O hash não toca em I/O. A captura de assinatura só acontece quando a ação **já
está** na lista de falhadas. Ação inédita — o caso normal — não paga nada:
verificado em teste, 5 cliques bem-sucedidos produzem **0** capturas.

### A exceção que os testes revelaram

Rodar a suíte completa reprovou a primeira versão em `t4` e `t12`, e uma das
reprovações apontou um erro de desenho real: **ações de espera não podem ser
guardadas**. Um `wait_text` que estourou em 5 s pode acertar em 7 s — recusá-lo
por "a tela não mudou agora" inverte o sentido dele. Mesma coisa para
`release_keys`, que tem que funcionar sempre, sobretudo repetido.

`ACOES_FORA_DA_GUARDA` = `wait`, `wait_for`, `wait_text`, `release_keys`,
`release_all`, `hover_probe`, `probe`.

Escape: `allow_repeat: true` no corpo; `REPEAT_GUARD=0` desliga tudo.

---

## Item 3 — Higiene de subprocesso, para a IDEIAS/04

A IDEIAS/04 propõe rodar um detector treinado de ícone **em processo separado**.
O `src/process.js` do jev-social é o checklist do que essa camada precisa, e é
melhor copiar a lista do que redescobrir cada item por bug:

| Cuidado | Por quê |
|---|---|
| **Allowlist de variáveis de ambiente** | o filho não herda o ambiente inteiro; só as chaves nomeadas |
| **Teto de bytes de saída, que MATA o processo** | detector em laço de erro enchendo stdout não derruba o servidor por memória |
| **`SIGTERM` → carência → `SIGKILL`** | e na **árvore** de processos, não só no filho direto |
| **`AbortSignal`** | cancelar de fora quando a captura já não interessa |
| **`windowsHide: true`** | sem janela de console piscando na tela que estamos capturando |
| **Timeout com `unref`** | o timer não segura o processo vivo |

O último é específico do Node, mas o equivalente em Python
(`subprocess` + `Popen.terminate()` → `kill()`, grupo de processos,
`threading.Timer(daemon=True)`) é o mesmo desenho.

Regra que fecha o conjunto, já escrita na IDEIAS/04: **detector morto ou lento
degrada para o detector clássico**, nunca derruba o `/state`.

---

## Problema conhecido, sem conserto pronto: nada é redigido

Isto não é uma ideia a implementar; é uma falha nossa que a leitura expôs.

O jev-social tem uma denylist de chaves que nunca chegam ao modelo nem ao
relatório:

```js
const PRIVATE_EVIDENCE_KEY =
  /^(?:stdout|stderr|cookie(?:s|_jar|_string)?|dom|dom_state|(?:inner_|outer_)?html
     |page_source|storage_state|(?:.*_)?headers|authorization|(?:.*_)?token
     |api_?key|secret|password|raw(?:_.*)?)$/i;
```

**Nós mandamos o texto OCR da tela inteira para modelos, sem nenhuma redação.**
Essa tela pode ter um `.env` aberto no editor, um terminal com token exportado,
um campo de senha em texto claro, uma chave de API num Inspector do Unity.

A solução deles não transfere: a denylist é sobre **chaves estruturadas** de um
JSON, e o nosso dado é **texto livre** reconhecido de pixels. Detectar segredo
em texto OCR é outro problema, com falso positivo caro nos dois sentidos
(redigir demais cega o agente; de menos vaza).

O que dá para fazer sem resolver o problema inteiro, em ordem de custo:

1. **Marca de campo de senha.** UIA expõe `IsPassword` em controles Win32. Onde
   a UIA enxerga — o que exclui o Unity —, dá para suprimir o texto da região.
2. **Padrões de alta confiança.** `sk-…`, `ghp_…`, `AKIA…`, `-----BEGIN …
   PRIVATE KEY-----`. Pouco falso positivo, cobertura pequena.
3. **Lista de janelas em que não se faz OCR**, por título ou processo
   (gerenciador de senhas, terminal com sessão marcada). O agente sabe que
   aquela região existe e é opaca, em vez de ler tudo.

Nenhuma é completa. O primeiro passo honesto é **documentar que hoje não há
redação nenhuma**, que é o que este parágrafo faz.

---

## O que não copiar

`mediaDownloadRequested()` infere intenção do usuário com regex bilíngue sobre
linguagem natural, com padrões de exclusão para não disparar em "comentários" ou
"评论". É engenhoso e é frágil no mesmo eixo em que já me queimei aqui — ajustar
limiar ao próprio exemplo (o corte de linha do grafo na IDEIAS/03, o dHash
cego a cor três vezes). Se precisarmos de gate por intenção, **perguntar é
melhor que regex**.

E o `saveRun` deles não tem o que ensinar: nosso `_save_som_trackers_locked` já
faz temp + `fsync` + `os.replace` com limpeza no erro; eles nem fazem `fsync`.

---

## Próximo passo

Nada aqui é urgente. Na ordem em que eu pegaria:

1. **Item 3** entra como seção de requisitos na IDEIAS/04, quando o detector for
   implementado — custo zero agora.
2. **Redação** — item 1 da lista (`IsPassword` via UIA) é pequeno e não depende
   de mais nada.
3. **Item 1 (`/actions`)** só depois de medir erro de composição por tarefa. Sem
   esse número é opinião.
