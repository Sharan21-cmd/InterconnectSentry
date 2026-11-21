import json
import os
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

XSDB = os.environ.get("INTERCONNECTSENTRY_XSDB", r"C:\AMDDesignTools\2025.2\Vivado\bin\xsdb.bat")
INIT = os.environ.get("INTERCONNECTSENTRY_PS7_INIT", str(Path(__file__).resolve().with_name("ps7_init.tcl")))
PORT = 8765
LOCK = threading.Lock()

def run_fpga(commands):
    script = "\n".join([
        "connect",
        "targets 2",
        f"source {INIT.replace(chr(92), '/')}",
        "ps7_init",
        "ps7_post_config",
        "mwr -force 0x41200004 0",
        "mwr -force 0x4120000C 0xFFFFFFFF",
        "mwr -force 0x41210004 0",
        "mwr -force 0x4121000C 0",
        *commands,
        "disconnect"
    ])

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tcl", encoding="ascii",
        delete=False
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        with LOCK:
            result = subprocess.run(
                ["cmd.exe", "/c", "call", XSDB, script_path],
                capture_output=True, text=True, timeout=45
            )

        output = result.stdout + result.stderr
        match = re.search(r"FPGA_STATUS=(\d+)", output)

        if result.returncode != 0 or not match:
            raise RuntimeError(output or "XSDB did not return FPGA status")

        value = int(match.group(1))
        state = (value >> 30) & 3

        return {
            "raw": hex(value),
            "status_code": state,
            "status": ["IDLE", "RUNNING", "DONE", "ERROR"][state],
            "localized": bool((value >> 29) & 1),
            "fault_detected": bool((value >> 28) & 1),
            "evidence_valid": bool((value >> 27) & 1),
            "transfers": value & 0x07FFFFFF
        }
    finally:
        Path(script_path).unlink(missing_ok=True)

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_json({"ok": True})

    def do_GET(self):
        if urlparse(self.path).path != "/api/status":
            return self.send_json({"error": "Unknown endpoint"}, 404)
        try:
            result = run_fpga([
                "set status [mrd -value -force 0x41200008]",
                'puts "FPGA_STATUS=$status"'
            ])
            self.send_json({"ok": True, **result})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            endpoint = urlparse(self.path).path

            if endpoint == "/api/start":
                workload = int(data.get("workload", 100))
                target = int(data.get("target_link", 0))
                fault_type = int(data.get("fault_type", 0))
                enable = int(bool(data.get("fault_enable", False)))
                mask = int(data.get("fault_mask", 0))
                delay = int(data.get("delay_cycles", 0))
                drop = int(data.get("drop_interval", 0))

                if not (1 <= workload <= 65535 and 0 <= target <= 15
                        and 0 <= fault_type <= 3 and 0 <= mask <= 0xFFFFFFFF
                        and 0 <= delay <= 255 and 0 <= drop <= 255):
                    return self.send_json({"error": "Invalid parameter"}, 400)

                ctrl = (workload << 16) | (target << 12) | (fault_type << 10) | (enable << 9)

                result = run_fpga([
                    f"mwr -force 0x41210000 0x{mask:08X}",
                    f"mwr -force 0x41210008 0x{((delay << 8) | drop):08X}",
                    f"mwr -force 0x41200000 0x{(ctrl | 0x40):08X}",
                    f"mwr -force 0x41200000 0x{ctrl:08X}",
                    "set status [mrd -value -force 0x41200008]",
                    'puts "FPGA_STATUS=$status"'
                ])
                return self.send_json({"ok": True, **result})

            if endpoint == "/api/stop":
                result = run_fpga([
                    "mwr -force 0x41200000 0x80",
                    "mwr -force 0x41200000 0",
                    "set status [mrd -value -force 0x41200008]",
                    'puts "FPGA_STATUS=$status"'
                ])
                return self.send_json({"ok": True, **result})

            return self.send_json({"error": "Unknown endpoint"}, 404)

        except Exception as e:
            return self.send_json({"ok": False, "error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print("[HTTP]", fmt % args)

print("InterconnectSentry FPGA Bridge")
print("Address: http://127.0.0.1:8765")
print("Keep this PowerShell window open.")

ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
