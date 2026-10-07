# Ideia 01 — Extrator Determinístico de Design Tokens

> **Status:** Ideia  
> **Data:** 2026-09-22  
> **Confiança:** 98%

---

## O Problema Real

Quando você diz *"quero um site igual a essa imagem"*, a IA não está sendo preguiçosa — ela é estruturalmente incapaz de replicar fidelidade visual sem dados concretos.

**Por que isso acontece:**

Os modelos de visão (ViT, CLIP, etc.) processam imagens em patches de 14×14 ou 16×16 px e comprimem cada patch num vetor de embedding. No processo, a cor exata `#1A0533` e a cor `#1B0635` colapsam para o mesmo cluster estatístico — são indistinguíveis no espaço latente. O modelo "sabe" que a cor é *roxo escuro*, mas não sabe *qual* roxo escuro.

O mesmo vale para:
- Espaçamentos (`padding: 24px` vs `padding: 28px`)
- Pesos tipográficos (`font-weight: 600` vs `700`)
- Border radius (`8px` vs `12px`)

**Analogia precisa:** A IA não vê preto e branco — ela vê *categorias estatísticas de cor*. Ela sabe que o fundo é "escuro e levemente roxo", mas não o valor hexadecimal exato.

---

## Por que Prompt Rígido NÃO Resolve

Aumentar a especificidade do prompt (`"use exatamente as mesmas cores"`) não elimina o problema porque:

1. A IA não tem como extrair `#1A0533` de uma imagem via linguagem — ela chuta dentro da distribuição estatística de imagens de treino.
2. O modelo pode "ver" que algo é azul e gerar `#3B82F6` (tailwind blue-500) por ser o mais frequente no treino — mesmo que a referência seja `#2D6BE4`.
3. Layouts são inferidos por proporção visual, não por px absolutos.

**Conclusão:** Mais prompt = menos erro, mas erro residual sempre existe. A única solução determinística é extrair os tokens antes de passar para a IA.

---

## A Ferramenta: Extrator Determinístico de Design Tokens

### Conceito

Um pipeline Python que recebe uma imagem de referência (screenshot, mockup, foto de site) e produz um arquivo `tokens.json` com todos os valores mensuráveis extraídos deterministicamente — sem suposição, sem média estatística.

A IA recebe o `tokens.json` junto com a imagem e trabalha com valores concretos, não com inferência.

---

## Arquitetura: 4 Etapas

```
[Imagem de Referência]
        │
        ▼
┌─────────────────────┐
│  1. Extração de Cor │  K-Means clustering → paleta exata em HEX + frequência %
└─────────────────────┘
        │
        ▼
┌──────────────────────────┐
│  2. Análise de Layout    │  Detecção de bordas + bounding boxes → grid, padding, gaps em px
└──────────────────────────┘
        │
        ▼
┌──────────────────────────┐
│  3. Extração Tipográfica │  OCR (Tesseract/EasyOCR) → famílias, tamanhos, pesos estimados
└──────────────────────────┘
        │
        ▼
┌──────────────────────────┐
│  4. Compilação JSON      │  tokens.json com tudo estruturado, pronto para IA consumir
└──────────────────────────┘
```

---

## Exemplo de Saída — `tokens.json`

```json
{
  "colors": {
    "background_primary": "#1A0533",
    "background_secondary": "#2C0F5E",
    "accent_primary": "#7B2FBE",
    "accent_secondary": "#A855F7",
    "text_primary": "#F3E8FF",
    "text_secondary": "#C4B5FD",
    "palette": [
      { "hex": "#1A0533", "frequency": 0.42, "role": "background" },
      { "hex": "#7B2FBE", "frequency": 0.18, "role": "accent" },
      { "hex": "#F3E8FF", "frequency": 0.15, "role": "text" }
    ]
  },
  "typography": {
    "heading": {
      "size_px": 48,
      "weight_estimated": 700,
      "line_height_ratio": 1.2,
      "sample_text": "Bem-vindo ao Portal"
    },
    "body": {
      "size_px": 16,
      "weight_estimated": 400,
      "line_height_ratio": 1.6
    }
  },
  "spacing": {
    "section_padding_top_px": 80,
    "section_padding_bottom_px": 80,
    "column_gap_px": 32,
    "card_padding_px": 24
  },
  "layout": {
    "grid_columns": 12,
    "content_max_width_px": 1200,
    "sidebar_width_px": null,
    "border_radius_card_px": 12,
    "border_radius_button_px": 8
  },
  "shadows": {
    "card_shadow": "0 4px 24px rgba(123, 47, 190, 0.25)"
  },
  "gradients": [
    {
      "type": "linear",
      "angle_deg": 135,
      "stops": ["#1A0533", "#2C0F5E"]
    }
  ],
  "metadata": {
    "source_image": "referencia.png",
    "resolution": "1920x1080",
    "extracted_at": "2026-09-22T10:00:00Z",
    "confidence": {
      "colors": 0.99,
      "layout": 0.87,
      "typography": 0.78
    }
  }
}
```

---

## Uso Prático com IA

**Fluxo atual (problemático):**
```
[Imagem] → IA → Site (cores aproximadas, layout genérico)
```

**Fluxo com a ferramenta:**
```
[Imagem] → Extrator → tokens.json
[Imagem + tokens.json] → IA → Site (cores exatas, layout determinístico)
```

O prompt para a IA muda de:
> *"Faça um site parecido com essa imagem"*

Para:
> *"Faça um site usando EXATAMENTE esses tokens: background `#1A0533`, accent `#7B2FBE`, card padding `24px`, border-radius `12px`, heading `48px weight 700`..."*

A IA não infere mais — ela recebe os valores e executa.

---

## Stack Técnica

| Componente | Biblioteca | Justificativa |
|---|---|---|
| K-Means de cores | `scikit-learn` + `Pillow` | Determinístico, rápido, sem GPU |
| Detecção de bordas/layout | `OpenCV` | Já disponível no ambiente |
| OCR tipográfico | `EasyOCR` ou `pytesseract` | EasyOCR tem melhor precisão em UI |
| Exportação | `json` nativo | Sem dependência extra |
| Interface CLI | `argparse` | Simples, direto |

---

## Resultado Esperado

| Métrica | Sem ferramenta | Com ferramenta |
|---|---|---|
| Fidelidade de cor | ~60–75% (suposição) | ~99% (determinístico) |
| Fidelidade de layout | ~50–70% (inferência) | ~85–92% (medição) |
| Fidelidade tipográfica | ~55–70% (estimativa) | ~75–85% (OCR) |
| Iterações necessárias | 4–8 ciclos | 1–2 ciclos |

---

## Limitações Conhecidas

- **OCR em fontes decorativas** tem precisão reduzida (~60%).
- **Gradientes complexos** (mais de 4 stops) perdem detalhes.
- **Efeitos de blur/glassmorphism** são difíceis de quantificar em px.
- **Animações e estados hover** estão fora do escopo desta versão.

---

## Próximos Passos

- [ ] MVP: extração de paleta de cores (K-Means, 6 clusters)
- [ ] Layout: detecção de blocos via `cv2.findContours`
- [ ] OCR: integração EasyOCR com filtragem de ruído
- [ ] CLI: `python extrator.py referencia.png --output tokens.json`
- [ ] Integração: instrução padrão para passar tokens.json junto com a imagem para a IA
