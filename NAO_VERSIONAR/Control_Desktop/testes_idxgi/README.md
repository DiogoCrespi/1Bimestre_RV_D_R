# Arquitetura de Aceleração Visual IDXGI & OCR Regional

## 1. Conclusão da Fase de Testes
A fase de testes, benchmarks e validação técnica do `IDXGI Output Duplication` e do `OCR Regional` está **encerrada e aprovada**.

> **Declaração de Conformidade Documental:**  
> *"Validado nos cenários E2E testados em Windows/Unity 6.5, com fallback conservador para Full OCR em qualquer incerteza estrutural."*

---

## 2. Princípios de Arquitetura Aprovados

1. **IDXGI como Oráculo de Invalidação**:
   - O oráculo DXGI rastreia dirty rects e move rects da saída gráfica com custo de CPU < 1.5%.
   - Cache hit apenas é emitido quando há evidência suficiente de tela inalterada na janela de interesse.
   - Singleton dentro do servidor para gerenciamento centralizado de lifecycle e tratamento de `DXGI_ERROR_ACCESS_LOST`.

2. **Settle Pós-Ação com Quiescência Visual**:
   - Toda ação gera um watermark de geração antes do disparo.
   - A atividade no alvo é distinguida de ruídos em outras janelas.
   - Requer estabilidade temporal (quiescência mínima de 40 ms a 100 ms) antes de liberar cache.
   - Distinção estrita mantida entre `temporally_quiescent` e `outcome_verified`.
   - Timeout máximo determinístico: fallback incondicional para Full Snapshot se excedido.

3. **OCR Regional Condicionado à Área e Topologia/Reflow**:
   - Critério estrito de ativação dupla:
     $$\text{OCR Regional Ativo} \iff (\text{Área Expandida} \le 20\%) \land (\text{Validação Estrutural} == \text{Safe})$$
   - **Margem Adaptativa**: computada dinamicamente como $1.5\times$ a altura média de linha ($15\text{ px}$ a $40\text{ px}$), impedindo fatiamento de glifos.
   - **Guarda Estrutural contra Reflow (`expand_for_reflow_containers`)**: mutações em colunas de layout vertical (Inspector à direita, Hierarchy/árvores à esquerda e Console/logs no rodapé) expandem a região para abranger toda a cadeia de elementos dependentes. Se ultrapassar o limiar de 20%, ativa fallback imediato para Full OCR.
   - **Detecção de Toque em Borda (`edge_touching_detected <= 8px`)**: qualquer caractere rente à borda externa do crop reprova a integridade e dispara Full OCR.

4. **Full OCR como Fonte Confiável e Fallback Definitivo**:
   - Zero tolerância para perda de marcas ou elementos fantasmas retidos.
   - Qualquer incerteza geométrica, scroll, redimensionamento ou reflow descarta o crop e executa Full OCR.

5. **Modo de Operação e Telemetria**:
   - `USE_REGIONAL_OCR = False` por padrão global (opt-in defensivo em produção).
   - Telemetria obrigatória por requisição:
     - `cache_hit` (boolean)
     - `regional_ocr` (boolean)
     - `fallback_full` (boolean)
     - `fallback_reason` (string, ex: `area_threshold_exceeded`, `structural_guard_edge_touching_text_detected`, etc.)
     - `latency_ms` (por caminho executado)

---

## 3. Resumo dos Benchmarks E2E (Unity 6.5)

| Cenário | Área | Modo Decidido | Motivo / Guarda | Equivalência | Perdidos | Obsoletos |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Menu / Popup Isolado** | 4.6% | Fallback Full OCR | Borda limítrofe (`edge_touching`) | 100.0% | 0 | 0 |
| **Foldout Inspector** | 29.8% (expandida) | Fallback Full OCR | Reflow container > 20% | 100.0% | 0 | 0 |
| **Scroll no Console** | 20.9% (expandida) | Fallback Full OCR | Deslocamento vertical > 20% | 100.0% | 0 | 0 |
| **Árvores / Hierarchy** | 22.0% (expandida) | Fallback Full OCR | Subordinação de nós > 20% | 100.0% | 0 | 0 |
| **Dois Painéis Simultâneos** | 50.4% | Fallback Full OCR | Área multirregional > 20% | 100.0% | 0 | 0 |
| **Botões / Badges Locais** | 2.5% | Regional OCR | Safe & within threshold | 100.0% | 0 | 0 |

---
**Status Final:** Fase concluída. Sem novas otimizações no IDXGI.
