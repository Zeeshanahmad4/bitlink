# start_services.py — Unified Process Supervisor for Bitlink
import os
import sys
import time
import subprocess
import threading
import signal

PYTHON_EXE = sys.executable
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICES = [
    {
        "name": "Node WhatsApp Gateway",
        "tag": "GATEWAY",
        "cmd": ["node", os.path.join(BASE_DIR, "whatsapp-bot", "service-baileys.js")],
        "cwd": os.path.join(BASE_DIR, "whatsapp-bot")
    },
    {
        "name": "WhatsApp Python Bridge",
        "tag": "WA-BRIDGE",
        "cmd": [PYTHON_EXE, os.path.join(BASE_DIR, "main_whatsapp.py")],
        "cwd": BASE_DIR
    },
    {
        "name": "Telegram Python Bridge",
        "tag": "TG-BRIDGE",
        "cmd": [PYTHON_EXE, os.path.join(BASE_DIR, "main_telegram.py")],
        "cwd": BASE_DIR
    },
    {
        "name": "Control Dashboard Server",
        "tag": "DASHBOARD",
        "cmd": [PYTHON_EXE, os.path.join(BASE_DIR, "control_server.py")],
        "cwd": BASE_DIR
    },
    {
        "name": "Slack App Manager",
        "tag": "MANAGER",
        "cmd": [PYTHON_EXE, os.path.join(BASE_DIR, "bitlink_manager.py")],
        "cwd": BASE_DIR
    }
]

processes = []
stop_flag = False


def stream_logs(proc, tag):
    """Streams child process stdout/stderr with clean colored tags."""
    colors = {
        "GATEWAY": "\033[32m",     # Green
        "WA-BRIDGE": "\033[36m",   # Cyan
        "TG-BRIDGE": "\033[34m",   # Blue
        "DASHBOARD": "\033[35m"    # Magenta
    }
    reset = "\033[0m"
    col = colors.get(tag, "")

    for line in iter(proc.stdout.readline, ''):
        if not line:
            break
        ts = time.strftime("%H:%M:%S")
        sys.stdout.write(f"{col}[{ts}] [{tag}]{reset} {line}")
        sys.stdout.flush()


def shutdown(signum=None, frame=None):
    """Gracefully terminates all child processes."""
    global stop_flag
    if stop_flag:
        return
    stop_flag = True
    print("\n🛑 [SUPERVISOR] Shutting down all Bitlink services...")
    for item in processes:
        proc = item["proc"]
        name = item["name"]
        try:
            print(f"   -> Stopping {name} (PID {proc.pid})...")
            proc.terminate()
        except Exception:
            pass
    time.sleep(1)
    for item in processes:
        proc = item["proc"]
        try:
            proc.kill()
        except Exception:
            pass
    print("✅ [SUPERVISOR] All services stopped cleanly.")
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("=" * 70)
    print("🚀 BITLINK UNIFIED PROCESS SUPERVISOR")
    print("   Starting Node Gateway (:3101), WhatsApp Bridge (:8101), Dashboard (:5050)")
    print("   Press Ctrl+C at any time to stop all services cleanly.")
    print("=" * 70)

    for svc in SERVICES:
        name = svc["name"]
        tag = svc["tag"]
        cmd = svc["cmd"]
        cwd = svc["cwd"]

        print(f"⚡ Launching {name}...")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )
            processes.append({"name": name, "proc": proc, "svc": svc})
            t = threading.Thread(target=stream_logs, args=(proc, tag), daemon=True)
            t.start()
        except Exception as e:
            print(f"❌ Failed to launch {name}: {e}")

    # Supervisor keep-alive loop with auto-restart
    try:
        while not stop_flag:
            time.sleep(2)
            for item in processes:
                proc = item["proc"]
                if proc.poll() is not None:
                    if stop_flag:
                        break
                    print(f"⚠️ [SUPERVISOR] Process {item['name']} exited with code {proc.returncode}. Auto-restarting in 3s...")
                    time.sleep(3)
                    if stop_flag:
                        break
                    try:
                        svc = item["svc"]
                        new_proc = subprocess.Popen(
                            svc["cmd"],
                            cwd=svc["cwd"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            bufsize=1
                        )
                        item["proc"] = new_proc
                        t = threading.Thread(target=stream_logs, args=(new_proc, svc["tag"]), daemon=True)
                        t.start()
                        print(f"✅ [SUPERVISOR] Relaunched {item['name']} (PID {new_proc.pid}).")
                    except Exception as restart_err:
                        print(f"❌ [SUPERVISOR] Failed to restart {item['name']}: {restart_err}")
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
