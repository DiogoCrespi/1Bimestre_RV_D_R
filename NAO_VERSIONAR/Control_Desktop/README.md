# Servidor de Controle Remoto Híbrido & Percepção de Interface (Windows)

Servidor de controle remoto determinístico e percepção visual em tempo real para agentes de IA interagirem com a área de trabalho do Windows (Unity Editor, Chrome, navegadores, Electron e apps nativos Win32).

---

## 🧭 Mapa do Repositório (Guia para IA e Desenvolvedores)

Se você é um agente de IA explorando este projeto, utilize este índice para encontrar os módulos corretos sem se perder:

```
├── remote_control_server.py      # [CORE] Servidor HTTP/REST de controle remoto e percepção visual
├── outcome_verifier.py           # [CORE] Verificador pós-ação semântico (actionDispatched vs outcomeVerified)
├── iniciar_controle_remoto.bat   # [LAUNCHER] Script para iniciar o servidor na porta 8000
├── AGENTS_PROTOCOL.md            # [CONTRATO] Documentação dos endpoints, parâmetros e payloads para LLMs
│
├── tests/                        # Suíte de testes automatizados e integração
│   ├── test_outcome_verifier_suite.py  # Testes dos 3 estados do OutcomeVerifier (verified/failed/unknown)
│   ├── test_server_outcome_contract.py # Testes de contrato E2E via execute_system_action
│   └── test_e2e_outcome_verifier.py    # Teste de integração ao vivo no desktop Windows
│
├── testes_idxgi/                 # Módulo de aceleração gráfica IDXGI Output Duplication e OCR Regional
│   ├── README.md                 # Documentação formal e validação E2E do oráculo DXGI
│   ├── dxgi_snapshot_service.py  # Serviço de oráculo de dirty rects e detecção de mudanças
│   └── regional_ocr_shadow.py    # Pipeline de OCR regional com guarda estrutural contra reflow
│
├── tools/                        # Utilitários de diagnóstico e inspeção de sistema
│   ├── listar_janelas.py         # Lista janelas ativas e HWNDs
│   ├── inspecionar_ambiente.py   # Diagnóstico de DPI, resoluções e monitores
│   └── inspecionar_unity_janelas.py # Mapeamento da hierarquia de janelas da Unity
│
├── benchmarks/                   # Histórico de benchmarks (Flicker WinRT OCR, Hover nativo, ruído)
├── templates/                    # Templates visuais para localização via OpenCV
├── docs/                         # Relatórios e análises arquiteturais aprofundadas
└── IDEIAS/ & REFERENCIAS/        # Notas de design, referências conceituais e roadmap
```

---

## 🚀 Como Iniciar

Execute o launcher na raiz do projeto:
```bat
iniciar_controle_remoto.bat
```
Ou diretamente via Python:
```bash
python remote_control_server.py
```
O servidor inicializa por padrão na porta `8000` (`http://localhost:8000`).

---

## 🤖 Guia Prático para Agentes de IA

### 1. Inspecionar o Estado da Tela (`/state`)
- `GET /state?mode=auto` (ou `POST /state`)
- Retorna:
  - `marks`: elementos visuais identificados por OCR (WinRT nativo ultra-rápido) com tags numéricas e bounding boxes.
  - `elements`: árvore de controles semânticos UIA (botões, inputs, abas).
  - `window`: metadados da janela ativa em primeiro plano (`hwnd`, título, dimensões).
  - `frame_id`: identificador imutável da captura para rastreabilidade de eventos.

### 2. Executar Ações com Verificação Semântica (`/act`)
Toda ação pode receber blocos opcionais de `precondition` e `expect`:

```json
POST /act
{
  "action": "click_tag",
  "tag": 14,
  "precondition": {
    "type": "window_focused",
    "target": "Unity"
  },
  "expect": {
    "type": "modal_opened",
    "target": "Save Changes",
    "timeout_ms": 1500
  }
}
```

### 3. Interpretação do Retorno (`OutcomeVerifier`):
- `ok`: indica que a requisição foi processada sem falhas de infraestrutura.
- `actionDispatched: true`: o input de mouse/teclado de baixo nível foi enviado ao SO.
- `outcomeVerified: true`: o resultado desejado foi **estritamente comprovado** na UI.
- `outcome.status`:
  - `"verified"`: pós-condição confirmada com evidência unívoca (vinculada a `frame_id`).
  - `"failed"`: evidência contrária real observada (ex: modal continuou aberto ou campo contém valor divergente).
  - `"unknown"`: timeout ou ambiguidade. **Atenção:** ausência de prova **NÃO** é falha. Nunca faça retry cego a partir de `unknown`.
- `outcome.drift`: se a interface sofreu reflow e o alvo mudou de lugar, retorna as novas coordenadas (`drift_detected: true`, `new_center: [x, y]`).

---

## 🧪 Execução dos Testes

Para validar a integridade do sistema:
```bash
# Testes do OutcomeVerifier
python -m unittest tests/test_outcome_verifier_suite.py

# Testes de Contrato da API
python tests/test_server_outcome_contract.py
```
