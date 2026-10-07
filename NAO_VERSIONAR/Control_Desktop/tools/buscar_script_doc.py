with open(r'C:\Users\Admin\Desktop\Desktop\IDEIAS\03_percepcao_de_tela.md', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        if 'context_menu' in line.lower() or 'scope' in line.lower():
            print(f"{i:4d}: {line.strip()[:100]}")
