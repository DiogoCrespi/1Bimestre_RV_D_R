import subprocess

def checar():
    out = subprocess.check_output(['git', '-C', r'C:\Users\Admin\Desktop\Desktop', 'show', 'origin/master:remote_control_server.py'], text=True, encoding='utf-8')
    lines = out.splitlines()
    for i, l in enumerate(lines, 1):
        if 'action in ("hover_probe", "probe"):' in l:
            print(f"Trecho encontrado em origin/master na linha {i}:")
            for j in range(max(0, i-2), min(len(lines), i+18)):
                print(f"{j+1:4d}: {lines[j]}")
            break

if __name__ == '__main__':
    checar()
