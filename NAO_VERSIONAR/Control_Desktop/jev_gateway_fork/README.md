# Jev Gateway & Guardrail (Fork de Automação e Programação)

Fork de integração entre o **Jev AI (TypeSafe System One)** e o atuador de desktop **`remote_control_server.py`**, inspirado na arquitetura de roteamento de ferramentas do repositório [`vinilana/jev-gateway`](https://github.com/vinilana/jev-gateway.git).

---

## 1. Objetivo da Arquitetura

Atuar como um **Gateway e Guardrail inteligente ("Gatware")** para pipelines de programação, agentes de código e desenvolvimento de jogos (Unity / Godot / C#):
1. **Gate 1 - Roteamento de Ferramentas (`Choice`):** Decide em menos de 800 ms se uma tarefa deve ser resolvida por atalhos/scripts mecânicos, por modelos leves ou se deve ser escalonada para modelos de raciocínio profundo (*System Two*).
2. **Gate 2 - Guardrail de Segurança (`Noul`):** Impede que ações destrutivas (como fechar editores sem salvar, matar processos críticos ou deletar diretórios) sejam executadas fisicamente pelo `remote_control_server.py`.
3. **Gate 3 - Validador de Compilação (`Score`):** Avalia logs de build da Unity / dotnet em 3 níveis sem precisar de LLMs pesados.

---

## 2. Estrutura do Fork

```
jev_gateway_fork/
├── docs/
├── src/
│   ├── jev_router.py          # Motor do Gateway (Jev System One)
│   └── rcs_client.py          # Cliente HTTP do remote_control_server
├── tests/
│   └── test_gateway.py        # Suíte de testes unitários automatizados
├── .env                       # Chave de API TypeSafe e configurações de porta
├── AGENTS_PROTOCOL.md         # Protocolo de comunicação de agentes
├── demo_gateway.py            # Demonstração prática interativa
├── outcome_verifier.py        # Verificador de templates e telas OpenCV
├── README.md                  # Esta documentação
└── remote_control_server.py   # Servidor HTTP Win32 de controle de desktop
```

---

## 3. Configuração e Dependências

As dependências principais já estão instaladas no ambiente Python:
```bash
pip install typesafe-sdk requests
```

O arquivo `.env` já contém a chave de API operacional do TypeSafe:
```ini
TYPESAFE_API_KEY=apikey_21645da99ca7b663484083f62d43f1f5fe2a_9bd6c2552575569430553f06384bcf70cb40b73c14ba9862599f0ce4c9aea64c
RCS_URL=http://127.0.0.1:8765
JEV_MODEL=jev-latest
```

---

## 4. Como Executar

### Executar os Testes Unitários:
```bash
python tests/test_gateway.py
```

### Executar a Demonstração Interativa:
```bash
python demo_gateway.py
```
