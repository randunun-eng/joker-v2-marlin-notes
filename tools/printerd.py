#!/usr/bin/env python3
"""
printerd -- persistent print service for the MagicMaker SLuban (JOKER V2, ESP32).

Why a daemon: a print must outlive the terminal that started it. This process
owns the serial port for its whole lifetime (reconnecting would reset the
ESP32) and exposes the printer over HTTP, so a browser tab, a script, or the
MCP server can all drive it without fighting over /dev/cu.usbserial-2120.

DEFAULT PATH IS USB STREAMING (mode="stream").

SD upload (M28 -> M29 -> M23 -> M24) is implemented but DOES NOT WORK on this
board: M28 opens the file correctly, then the board resets after ~8-9 written
lines, every time. Ruled out by measurement: pacing (5/20/50 ms identical),
Wi-Fi contention ([ESP110]OFF identical), and G-code content (stripping Klipper
macros made it fail one line EARLIER). See docs/USB-PRINTING.md. To get the
robustness benefit of printing from the card, copy the file with a card reader
and start it from the LCD instead.

Constraints baked in, all verified against this firmware:
  * 8.3 FILENAMES ONLY. LONG_FILENAME_HOST_SUPPORT is disabled, so uploads are
    renamed to eight characters plus .GCO.
  * No BINARY_FILE_TRANSFER, so upload is ASCII and runs ~25-45 min for a 20 MB
    file. That is expected, not a fault.
  * Opening the port resets the board. We open once, at startup.
  * M112 is NOT immediate (EMERGENCY_PARSER is impossible on ESP32). Abort
    queues behind buffered moves; the power switch is the only real stop.

Run:  ./printerd.py            then open http://127.0.0.1:8765
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usb_print as up  # protocol helpers, reused rather than duplicated

HOST, PORT = "127.0.0.1", 8765
GCODE_DIRS = [
    "/Volumes/Backup/magicmaker 3d printer/3d print models",
    "/Volumes/Backup/magicmaker 3d printer/source",
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Downloads"),
]


def sd_name(path):
    """
    Marlin here has no long-filename support: 8 chars + .GCO, uppercase.

    Naive truncation collides -- 'eiffel_final_PLA_4h11m' and
    'eiffel_final_PLA_9h50m' both become EIFFELFI. A collision would silently
    overwrite the wrong file on the card, including one being printed. So the
    last 4 characters are a hash of the full path: stable across runs, distinct
    per file.
    """
    stem = re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(os.path.basename(path))[0])
    tag = hashlib.md5(os.path.abspath(path).encode()).hexdigest()[:4].upper()
    return ((stem[:4] or "PRNT").upper() + tag) + ".GCO"


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.d = {
            "status": "idle",      # idle|uploading|printing|paused|done|error
            "mode": None,          # sd|stream
            "file": None,
            "sd_file": None,
            "phase": "",
            "pct": 0.0,
            "line": 0,
            "total": 0,
            "started": None,
            "elapsed": 0.0,
            "eta": 0.0,
            "temps": "",
            "error": None,
            "connected": False,
            "log": [],
        }

    def get(self):
        with self.lock:
            return dict(self.d)

    def set(self, **kw):
        with self.lock:
            self.d.update(kw)

    def log(self, msg):
        with self.lock:
            self.d["log"] = (self.d["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-200:]


class Daemon:
    def __init__(self, port, baud):
        self.port, self.baud = port, baud
        self.st = State()
        self.pr = None
        self.job = None
        self.pause_ev = threading.Event()
        self.abort_ev = threading.Event()
        self.serial_lock = threading.Lock()

    # -- connection ------------------------------------------------------
    def connect(self):
        """Opening resets the ESP32, so this happens once."""
        self.pr = up.Printer(self.port, self.baud, reset=True, verbose=False)
        self.st.log("port open; waiting for Marlin")
        fw = self.pr.wait_boot()
        self.st.log(f"firmware: {fw or 'no banner'}")
        with self.serial_lock:
            self.pr.send_and_wait(0, "M110 N0")
        self.st.set(connected=True)
        threading.Thread(target=self._idle_temps, daemon=True).start()

    def _idle_temps(self):
        """
        Poll M105 while no job is running, so the live view shows real
        temperatures at idle instead of blanks. During a job this stays quiet:
        streaming already carries temps, and the SD watcher owns M27.
        """
        while True:
            time.sleep(3)
            st = self.st.get()
            if not st["connected"] or st["status"] in ("uploading", "printing", "paused"):
                continue
            try:
                with self.serial_lock:
                    self.pr.send_raw("M105")
                    deadline = time.time() + 4
                    while time.time() < deadline:
                        line = self.pr.readline()
                        if line is None:
                            continue
                        if "T:" in line:
                            self.st.set(temps=line.replace("ok ", "").strip())
                        if line.lower().startswith("ok"):
                            break
            except Exception:
                pass

    # -- jobs ------------------------------------------------------------
    def start(self, path, mode="stream"):
        cur = self.st.get()["status"]
        if cur in ("uploading", "printing", "paused"):
            raise RuntimeError(f"busy: {cur}")
        if not os.path.isfile(path):
            raise RuntimeError(f"no such file: {path}")

        cmds = up.load_gcode(path)
        ok, report = up.check_bounds(cmds, up.DEFAULT_BED)
        if not ok:
            raise RuntimeError("out of bounds for 125x125x145: " + " ".join(report[-4:]))

        self.abort_ev.clear()
        self.pause_ev.clear()
        self.st.set(status="uploading" if mode in ("sd", "upload") else "printing",
                    mode=mode, file=path,
                    sd_file=sd_name(path) if mode in ("sd", "upload") else None,
                    pct=0.0, line=0, total=len(cmds), started=time.time(),
                    error=None, phase="starting")
        self.job = threading.Thread(target=self._run, args=(cmds, mode), daemon=True)
        self.job.start()

    def _tick(self, i, total, phase):
        now = time.time()
        started = self.st.get()["started"] or now
        el = now - started
        frac = i / total if total else 0
        self.st.set(line=i, pct=frac * 100, elapsed=el,
                    eta=(el / frac - el) if frac > 0.001 else 0,
                    phase=phase, temps=self.pr.temps)

    def _send_collect(self, n, cmd, timeout=20):
        """Send a numbered command and collect every response line up to 'ok'."""
        out = []
        with self.serial_lock:
            self.pr.send_raw(up.frame(n, cmd))
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.pr.readline()
                if line is None:
                    continue
                if line.lower().startswith("ok"):
                    break
                out.append(line)
        return out

    def _open_sd_file(self, n, name):
        """
        M28, and PROVE it opened. If the card is missing or the open fails,
        Marlin leaves card.flag.saving false and silently executes every
        following line as real G-code -- heaters and motion instead of a file
        write. Never stream after an unconfirmed M28.
        """
        resp = self._send_collect(n, f"M28 {name}")
        if not any("writing to file" in l.lower() for l in resp):
            raise RuntimeError(
                "M28 did not confirm 'Writing to file' -- refusing to stream. "
                "Without an open file every line would EXECUTE (heaters + motion). "
                "Check an SD card is inserted. Marlin said: "
                + (" | ".join(resp[:8]) or "<nothing>"))
        self.st.log(f"M28 confirmed: {resp[0] if resp else name}")

    def _m20(self, timeout=25):
        """Read the SD listing. Used to prove an upload actually landed."""
        out = []
        with self.serial_lock:
            self.pr.send_raw("M20")
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.pr.readline()
                if line is None:
                    continue
                if line.lower().startswith("ok"):
                    break
                out.append(line)
        return out

    def _run_upload(self, cmds):
        """
        Upload only: M28 -> stream -> M29 -> M20 verify. Deliberately does NOT
        send M23/M24, so nothing heats and nothing moves. This is how the SD
        path gets proven without committing to a print.
        """
        name = self.st.get()["sd_file"]
        total = len(cmds)
        self.st.log(f"UPLOAD-ONLY: {total} lines -> {name} (no print will start)")
        n = 1
        self._open_sd_file(n, name); n += 1
        for i, c in enumerate(cmds, start=1):
            if self.abort_ev.is_set():
                self._send(n, "M29")
                self.st.set(status="idle", phase="upload aborted")
                return
            self._send(n, c); n += 1
            if i % 500 == 0 or i == total:
                self._tick(i, total, f"uploading -> {name}")
        self._send(n, "M29")
        self.st.log("M29 sent, verifying with M20")

        listing = self._m20()
        hit = [l for l in listing if name.split(".")[0].lower() in l.lower()]
        if hit:
            self.st.set(status="done", phase=f"VERIFIED on SD: {hit[0]}", pct=100.0)
            self.st.log(f"VERIFIED: {hit[0]}")
        else:
            self.st.set(status="error", pct=100.0,
                        error=f"{name} not found in M20 listing")
            self.st.log("VERIFY FAILED. M20 returned: " + " | ".join(listing[:12]))

    def _run(self, cmds, mode):
        try:
            if mode == "upload":
                self._run_upload(cmds)
            elif mode == "sd":
                self._run_sd(cmds)
            else:
                self._run_stream(cmds)
        except Exception as exc:
            self.st.set(status="error", error=str(exc))
            self.st.log(f"ERROR: {exc}")

    def _send(self, n, cmd):
        with self.serial_lock:
            r = self.pr.send_and_wait(n, cmd)
            while r != "ok":
                r = self.pr.send_and_wait(n, cmd)

    def _run_sd(self, cmds):
        name = self.st.get()["sd_file"]
        total = len(cmds)
        self.st.log(f"uploading {total} lines to SD as {name}")
        n = 1
        self._open_sd_file(n, name); n += 1

        for i, c in enumerate(cmds, start=1):
            if self.abort_ev.is_set():
                self._send(n, "M29")
                self.st.set(status="idle", phase="upload aborted")
                self.st.log("upload aborted before printing; nothing was printed")
                return
            self._send(n, c); n += 1
            if i % 200 == 0 or i == total:
                self._tick(i, total, f"uploading -> {name}")

        self._send(n, "M29"); n += 1
        self.st.log("upload complete (M29)")

        self._send(n, f"M23 {name}"); n += 1
        self._send(n, "M24"); n += 1
        self.st.set(status="printing", phase=f"printing {name} from SD", pct=0.0)
        self.st.log(f"printing {name} from SD -- host is now optional")
        self._watch_sd()

    def _watch_sd(self):
        """Poll M27. The print lives on the card now; we are only observing."""
        while not self.abort_ev.is_set():
            time.sleep(5)
            try:
                with self.serial_lock:
                    self.pr.send_raw("M27")
                    deadline = time.time() + 6
                    while time.time() < deadline:
                        line = self.pr.readline()
                        if line is None:
                            continue
                        m = re.search(r"SD printing byte (\d+)/(\d+)", line)
                        if m:
                            cur, tot = int(m.group(1)), int(m.group(2))
                            frac = cur / tot if tot else 0
                            started = self.st.get()["started"] or time.time()
                            el = time.time() - started
                            self.st.set(pct=frac * 100, elapsed=el,
                                        eta=(el / frac - el) if frac > 0.001 else 0)
                            if tot and cur >= tot:
                                self.st.set(status="done", phase="print finished", pct=100.0)
                                self.st.log("SD print finished")
                                return
                        if "Not SD printing" in line:
                            self.st.set(status="done", phase="print finished")
                            return
                        if line.lower().startswith("ok"):
                            break
            except Exception as exc:
                self.st.log(f"M27 poll failed: {exc}")
                return

    def _run_stream(self, cmds):
        total = len(cmds)
        self.st.log(f"streaming {total} lines over USB (host must stay awake)")
        for i, c in enumerate(cmds, start=1):
            if self.abort_ev.is_set():
                self._shutdown_heaters()
                self.st.set(status="idle", phase="aborted")
                return
            while self.pause_ev.is_set() and not self.abort_ev.is_set():
                self.st.set(status="paused", phase="paused")
                time.sleep(0.3)
            if self.st.get()["status"] == "paused":
                self.st.set(status="printing")
            self._send(i, c)
            if i % 20 == 0 or i == total:
                self._tick(i, total, "streaming over USB")
        self.st.set(status="done", phase="stream complete", pct=100.0)

    def _shutdown_heaters(self):
        try:
            with self.serial_lock:
                for c in ("M104 S0", "M140 S0", "M107", "M84"):
                    self.pr.send_raw(c)
                    time.sleep(0.2)
        except Exception:
            pass

    def abort(self):
        self.abort_ev.set()
        st = self.st.get()
        if st["status"] == "printing" and st["mode"] == "sd":
            # The card is driving; M25 pauses it, then heaters off.
            with self.serial_lock:
                self.pr.send_raw("M25")
            time.sleep(0.3)
        self._shutdown_heaters()
        self.st.set(status="idle", phase="aborted")
        self.st.log("ABORT sent. NOT instant -- M112 cannot be immediate on ESP32. "
                    "Use the power switch if the head is crashing.")


def list_gcode():
    out = []
    for d in GCODE_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith((".gcode", ".gco")):
                p = os.path.join(d, fn)
                try:
                    cmds = up.load_gcode(p)
                    ok, _ = up.check_bounds(cmds, up.DEFAULT_BED)
                except Exception:
                    ok = False
                out.append({"path": p, "name": fn,
                            "mb": round(os.path.getsize(p) / 1e6, 1),
                            "fits": ok, "sd_name": sd_name(p)})
    return out


UI = """<!doctype html><meta charset=utf-8><title>JOKER V2</title>
<style>
:root{color-scheme:dark}
body{background:#111;color:#ddd;font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:17px;margin:0 0 4px}.sub{color:#888;font-size:12px;margin-bottom:20px}
.card{background:#1b1b1b;border:1px solid #2c2c2c;border-radius:10px;padding:16px;margin-bottom:14px}
.bar{height:22px;background:#000;border-radius:5px;overflow:hidden;margin:10px 0}
.fill{height:100%;background:linear-gradient(90deg,#0a7,#0d9);width:0;transition:width .4s}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:10px}
.k{color:#888;font-size:11px;text-transform:uppercase}.v{font-size:15px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}td{padding:6px 4px;border-bottom:1px solid #262626}
button{background:#0a7;border:0;color:#000;font-weight:600;padding:6px 12px;border-radius:6px;cursor:pointer}
button.bad{background:#c33;color:#fff}button:disabled{background:#333;color:#666;cursor:not-allowed}
.bad-row{color:#c66}.sub2{color:#888;font-size:11px;margin:4px 0 8px}pre{background:#000;padding:10px;border-radius:6px;max-height:180px;overflow:auto;font-size:11px;margin:0}
</style>
<h1>JOKER V2 &mdash; print service</h1>
<div class=sub>Prints stream over USB &mdash; keep this Mac awake and the cable undisturbed. SD upload over serial is broken on this board (resets after ~8 lines), so to print from the card, copy the file with a card reader and start it from the LCD.</div>
<div class=card>
  <div id=phase>connecting&hellip;</div>
  <div class=bar><div class=fill id=fill></div></div>
  <div class=grid>
    <div><div class=k>status</div><div class=v id=status>-</div></div>
    <div><div class=k>progress</div><div class=v id=pct>-</div></div>
    <div><div class=k>elapsed</div><div class=v id=el>-</div></div>
    <div><div class=k>eta</div><div class=v id=eta>-</div></div>
  </div>
  <div style="margin-top:10px"><div class=k>temperatures</div><div class=v id=temps>-</div></div>
  <div style=margin-top:12px><button class=bad onclick=abort()>Abort</button></div>
</div>
<div class=card><b>Files on this Mac</b>
  <div class=sub2>Sliced G-code found locally. The arrow shows the 8.3 name it would take on the card &mdash; these are NOT on the SD card.</div>
  <table id=files></table></div>
<div class=card><b>On the printer's SD card</b>
  <button onclick=refreshSD()>Read card (M20)</button>
  <div class=sub2>Files here can be started from the printer's own LCD, independent of this Mac.</div>
  <table id=sd></table></div>
<div class=card><b>Log</b><pre id=log></pre></div>
<script>
// Always resolve elements explicitly. Implicit globals look tempting but
// window.status is a legacy STRING property, so `status.textContent = x`
// silently does nothing and the field never updates.
const $=id=>document.getElementById(id);
const fmt=s=>{s=Math.max(0,s|0);const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}:${String(m).padStart(2,'0')}`:`${m}m ${s%60}s`};
async function tick(){
 const r=await fetch('/api/status').then(r=>r.json());
 $('status').textContent=r.status; $('phase').textContent=r.phase||r.status;
 $('pct').textContent=r.pct.toFixed(1)+'%'; $('fill').style.width=r.pct+'%';
 $('el').textContent=fmt(r.elapsed); $('eta').textContent=fmt(r.eta);
 $('temps').textContent=r.temps||'-';
 const lg=$('log'); lg.textContent=(r.log||[]).join('\\n'); lg.scrollTop=lg.scrollHeight;
}
async function files(){
 const r=await fetch('/api/files').then(r=>r.json());
 document.getElementById('files').innerHTML=r.map(f=>`<tr class="${f.fits?'':'bad-row'}">
  <td>${f.name}</td><td>${f.mb} MB</td><td>${f.fits?'&rarr; '+f.sd_name:'OUT OF BOUNDS'}</td>
  <td style=text-align:right>${f.fits?`<button onclick="go('${f.path.replace(/'/g,"\\\\'")}')">Print</button>`:''}</td></tr>`).join('');
}
async function go(p){ if(!confirm('Upload to SD and print?\\n\\n'+p))return;
 const r=await fetch('/api/print',{method:'POST',body:JSON.stringify({path:p})});
 if(!r.ok)alert(await r.text()); }
async function abort(){ if(confirm('Abort? NOT instant - use the power switch if the head is crashing.'))
 await fetch('/api/abort',{method:'POST'}); }
async function refreshSD(){
 const t=document.getElementById('sd'); t.innerHTML='<tr><td>reading&hellip;</td></tr>';
 const r=await fetch('/api/sd').then(r=>r.json());
 if(r.busy){t.innerHTML='<tr><td>busy printing &mdash; not reading the card</td></tr>';return}
 t.innerHTML=r.files.length?r.files.map(f=>`<tr><td>${f.name}</td><td style=text-align:right>${
   f.bytes?(f.bytes/1e6).toFixed(2)+' MB':'<span class=bad-row>0 bytes (empty)</span>'}</td></tr>`).join('')
   :'<tr><td>no files</td></tr>';
}
setInterval(tick,1000); tick(); files(); refreshSD();
</script>"""


class Handler(BaseHTTPRequestHandler):
    daemon_ref = None

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = urlparse(self.path).path
        d = self.daemon_ref
        if p == "/":
            return self._send(200, UI, "text/html; charset=utf-8")
        if p == "/api/status":
            return self._send(200, json.dumps(d.st.get()))
        if p == "/api/files":
            return self._send(200, json.dumps(list_gcode()))
        if p == "/api/sd":
            # M20 costs serial time, so only when nothing is running.
            if d.st.get()["status"] in ("uploading", "printing", "paused"):
                return self._send(200, json.dumps({"busy": True, "files": []}))
            try:
                rows = []
                for l in d._m20():
                    parts = l.rsplit(" ", 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        rows.append({"name": parts[0], "bytes": int(parts[1])})
                return self._send(200, json.dumps({"busy": False, "files": rows}))
            except Exception as exc:
                return self._send(200, json.dumps({"busy": False, "files": [],
                                                   "error": str(exc)}))
        self._send(404, "{}")

    def do_POST(self):
        p = urlparse(self.path).path
        d = self.daemon_ref
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or "{}") if n else {}
        try:
            if p == "/api/print":
                d.start(body["path"], body.get("mode", "stream"))
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/abort":
                d.abort()
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/pause":
                d.pause_ev.set()
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/resume":
                d.pause_ev.clear()
                return self._send(200, json.dumps({"ok": True}))
        except Exception as exc:
            return self._send(400, str(exc), "text/plain")
        self._send(404, "{}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=up.DEFAULT_PORT)
    ap.add_argument("--baud", type=int, default=up.DEFAULT_BAUD)
    ap.add_argument("--http-port", type=int, default=PORT)
    ap.add_argument("--no-connect", action="store_true",
                    help="serve the UI without opening the serial port")
    args = ap.parse_args()

    d = Daemon(args.port, args.baud)
    if not args.no_connect:
        try:
            d.connect()
        except Exception as exc:
            print(f"serial connect failed: {exc}", file=sys.stderr)
            print("If a print is already running elsewhere, that process owns the port.",
                  file=sys.stderr)
            return 1
    else:
        d.st.log("started with --no-connect; serial is NOT open")

    Handler.daemon_ref = d
    srv = ThreadingHTTPServer((HOST, args.http_port), Handler)
    print(f"printerd on http://{HOST}:{args.http_port}  (serial: "
          f"{'open ' + args.port if not args.no_connect else 'NOT connected'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down (a running SD print continues on the printer)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
