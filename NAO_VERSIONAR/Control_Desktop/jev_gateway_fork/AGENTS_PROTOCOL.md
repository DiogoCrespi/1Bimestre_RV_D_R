# INSTRUÇÕES CRÍTICAS PARA AGENTES DE IA (CONTROLE DE TELA E UNITY)

> [!IMPORTANT]
> **PROIBIDO TIRAR SCREENSHOTS REPETIDAS EM DISCO.**
> Salvar JPEGs e inspecionar a tela cegamente causa lentidão severa (300-1000ms), superaquece o processador de 15W e gera erros sistemáticos de clique por suposição de coordenadas.

---

## 1. O Pipeline Ultrarrápido (OpenCV em Memória)

Para clicar em botões, abas ou menus, use o motor acelerado Win32 + OpenCV:
`C:\Users\Admin\Desktop\Desktop\remote_control_server.py`

### 1.1. Verificar Templates Prontos
Antes de tentar clicar em qualquer coisa, veja se o botão já está calibrado:
```powershell
python remote_control_server.py --list-templates
```
Templates já disponíveis na pasta `templates/`:
* `unity_play`: Botão de Play da Unity (ícone triangular).
* `unity_pause`: Botão de Pause da Unity.
* `unity_step`: Botão de Frame Step da Unity.

### 1.2. Clicar em Botão por Template (1 ms / 100% de precisão)
```powershell
python remote_control_server.py --click-template unity_play --window "Unity"
```
* O parâmetro `--window "Unity"` restringe a busca à janela, reduzindo o tempo de OpenCV para **menos de 15 ms**.
* O clique é executado de forma atômica no centro exato do ícone.

### 1.3. Clicar em Elementos de Texto (UIA / OCR Nativo)
Se o botão contiver texto (ex: menus "File", "Edit", botões do Inspector):
```powershell
python remote_control_server.py --click-text "File" --window "Unity"
python remote_control_server.py --click-text "Build Settings" --window "Unity"
```

### 1.4. Cliques Relativos à Janela (Imunes a bordas maximizadas -9,-9)
Se precisar clicar em uma porcentagem da janela, use coordenadas da área cliente:
```powershell
# Clica a 50% da largura e 10% da altura da janela da Unity
python remote_control_server.py --client-rx 0.50 --client-ry 0.10 --window "Unity"
```

### 1.5. Arrastar e Soltar (Drag and Drop OLE Robusto)
O arrasto foi calibrado com micro-deslocamento inicial de 5px (ativação de limiar OLE no Windows/Unity) e tempo de retenção (dwell time de 120ms no destino antes do soltar):
```powershell
# 1. Arrastar entre dois templates/ícones (ex: arrastar prefab ou elemento)
python remote_control_server.py --drag-template unity_play unity_pause --window Unity

# 2. Arrastar entre dois textos detectados na tela (OCR em RAM)
python remote_control_server.py --drag-text "PlayerPrefab" "TargetSlot" --window Unity

# 3. Arrastar por coordenadas da área cliente (0.0 a 1.0 da janela, imune a bordas DWM)
python remote_control_server.py --drag-client-rel 0.2 0.8 0.8 0.2 --window Unity --duration 0.3
```

### 1.6. Atalhos de Teclado e Digitação
```powershell
# Toggle Play/Stop na Unity via atalho
python remote_control_server.py --hotkey "ctrl+p" --window "Unity"

# Digitar texto com colagem ultrarrápida via clipboard
python remote_control_server.py --type "MinhaClasse.cs" --paste --enter --window "Unity"
```

---

## 2. Custo de Imagem: Prefira `/crop` ao Frame Inteiro

O modelo de visão enxerga a imagem em blocos de **28×28 px**. Uma imagem custa
`⌈largura/28⌉ × ⌈altura/28⌉` tokens visuais — ou seja, **o custo é a área**, e
mandar a tela inteira para ler um botão é desperdício puro.

| O que você manda | Dimensão | Tokens visuais |
|---|---|---|
| `/screenshot` ou `/som_frame` (tela cheia) | 1920×1080 | **2691** |
| `/crop` do Inspector da Unity | 600×800 | **638** (4,2× mais barato) |
| `/crop` de um botão ou diálogo | 400×300 | **165** (16× mais barato) |
| Tela cheia em 4K | 3840×2160 | **10764** |

```powershell
# Caro: manda a tela toda para achar um botão
python remote_control_server.py --screenshot

# Barato: manda só o painel que interessa (o --crop-out .png já sai em PNG)
python remote_control_server.py --crop 1200 100 600 800 --crop-out inspector.png
```

### Redução automática: quando o texto fica ilegível

Imagem acima do limite do modelo é **reduzida antes de ser processada** — e aí
texto pequeno e número de tag do SoM ficam ilegíveis.

| Tier | Modelos | Long edge máx | Tokens visuais máx |
|---|---|---|---|
| Alta resolução | Claude 4.7 e posteriores | 2576 px | 4784 |
| Padrão | Todos os outros | 1568 px | 1568 |

O tier é por **geração do modelo**, não por família — Sonnet 5 e Opus 5 estão
ambos no tier alto; Haiku 4.5, por ser anterior ao 4.7, fica no padrão.

* **Monitor 1080p em modelo 4.7+:** passa inteiro, sem redução. Nada a fazer.
* **Monitor 4K:** é reduzido para 2576×1449. Para ler detalhe, `/crop` obrigatório.
* **Modelo do tier padrão:** mesmo 1920×1080 cai para 1456×819. Aí `/crop` deixa
  de ser economia e vira requisito para conseguir ler.

### Teto de 20 imagens por requisição

Acima de **20 blocos de imagem na mesma requisição**, vale um limite por imagem
mais apertado: cada uma precisa caber em **2000 px por lado**, senão a requisição
é **rejeitada com erro** — não reduzida. Contam as imagens de turnos anteriores
que você reenvia no histórico, e isso acumula rápido num laço de agente.

* 1920×1080 passa (os dois lados abaixo de 2000).
* 3840×2160 **quebra**. Em 4K, mande sempre `/crop` dentro do laço.

### PNG para ler, JPEG para acompanhar

Compressão JPEG pesada degrada a legibilidade de texto. O `/frame` sai em quality
90 e o `/crop` em 95: bom para acompanhar a tela ao vivo, ruim para o modelo ler
texto miúdo. Quando o objetivo é **ler**, peça PNG:

```
POST /crop  {"bbox": "1200,100,600,800", "format": "png"}
```

Na CLI, basta a extensão: `--crop-out inspector.png` já sai em PNG.

---

## 3. Como Adicionar Novos Templates
Se você precisar clicar em um ícone novo que ainda não está na pasta `templates/`:
1. Recorte a região do ícone (ex: 30x30 pixels) uma única vez.
2. Salve em `templates/<nome_do_icone>.png`.
3. Use diretamente: `python remote_control_server.py --click-template <nome_do_icone> --window <Janela>`.

---

## 4. Leitura Parcial: `truncation` Não É Aviso Decorativo

**Se a resposta tem `truncation`, você não viu a tela inteira.** A conclusão
proibida é a mais natural: *"procurei e o elemento não está aí"*. Numa leitura
truncada essa frase é indefensável — o elemento pode estar entre os que não
foram coletados.

```json
"truncation": {
  "truncated": true,
  "reason": "limite_de_marcas",
  "collected": 300,
  "limit": 300,
  "stage": "icones"
}
```

### Os motivos, e o que cada um significa

| `reason` | O que aconteceu |
|---|---|
| `limite_de_marcas` | o SoM bateu em `SOM_MAX_MARKS` (300). `stage` diz se cortou no texto ou nos ícones — cortou no texto significa que **nenhum ícone foi coletado** |
| `limite_de_elementos` | a árvore UIA bateu em `UIA_MAX_ELEMENTS` (80) |
| `tempo_esgotado` | a varredura UIA estourou o prazo; o que veio é o que deu tempo |
| `thread_travada` | a varredura UIA não respondeu nem ao pedido de parada; o mais desconfiável de todos |

### O que fazer

1. **Não conclua ausência.** Um `click_text` que não acha o alvo numa leitura
   truncada não prova que o alvo não existe. Estreite antes de desistir.
2. **Estreite a leitura** em vez de subir o teto. `scope="active_dialog"`,
   `scope="context_menu"`, `scope="window"` ou uma `region` explícita cortam
   por relevância; subir `SOM_MAX_MARKS` corta por sorte e custa token.
3. **Diga que a leitura foi parcial** quando reportar o que viu. "Não encontrei
   o botão" e "não encontrei o botão nas 300 primeiras marcas de uma tela que
   tem mais" são afirmações diferentes.
4. **`stage: "texto"`** merece atenção especial: o corte aconteceu antes da
   passagem de ícones, então a leitura não tem ícone nenhum. Numa toolbar do
   Unity isso é quase tudo que importa.

### O que o servidor já garante

- **Nunca manda diff sobre coleta parcial.** Um `/state?since=` sobre leitura
  truncada volta completo, com `fullReason: "captura_truncada"`. Diff sobre
  coleta incompleta descreveria como `removed` marcas que simplesmente não
  foram coletadas.
- **O aviso sobrevive ao cache.** Inclusive no cache-hit do oráculo DXGI.

Isso protege a consistência do protocolo. **Não protege a sua conclusão** —
essa parte é sua.
