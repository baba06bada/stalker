"""Proxy sunucusunu baslat ve calisana kadar bekle."""
import subprocess, sys, time, os, socket

script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)
python_exe = sys.executable
proxy_script = os.path.join(script_dir, 'proxy_server.py')

proc = subprocess.Popen(
    [python_exe, proxy_script],
    stdout=open(os.path.join(script_dir, 'proxy_running.txt'), 'w'),
    stderr=subprocess.STDOUT
)

for _ in range(20):
    time.sleep(0.5)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(('127.0.0.1', 8000))
        s.close()
        print(f'Proxy started (PID {proc.pid})')
        break
    except:
        pass
else:
    print('Proxy failed to start')
    proc.terminate()

print('Press Ctrl+C to stop proxy')
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    proc.terminate()
    print('Proxy stopped')
