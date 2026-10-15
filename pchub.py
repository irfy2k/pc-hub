"""PC Hub: local dashboard for health checks, space cleanup and startup apps.

Run:  python pchub.py            (opens http://127.0.0.1:8765)
Test: python pchub.py --selftest
Stdlib only. Binds to 127.0.0.1; every /api call needs the per-run token
that is injected into the page, and the Host header must be local.
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
TOKEN = secrets.token_urlsafe(24)
HERE = Path(__file__).parent
MIN_AGE = 86400  # cleanup only deletes files untouched for a day, so open apps keep their files

LOCAL = os.environ["LOCALAPPDATA"]
HOME = Path.home()


def ps(script):
    """Run a PowerShell snippet that prints JSON and return the parsed result."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return json.loads(out.stdout or "null")


# ---------- Health ----------

HEALTH_PS = r"""
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$lp = [Environment]::ProcessorCount
$before = @{}; Get-Process | ForEach-Object { $before[$_.Id] = $_.CPU }
Start-Sleep -Milliseconds 1000
$procs = Get-Process | Where-Object Id -ne 0 | ForEach-Object {
  $d = 0; if ($_.CPU -and $before.ContainsKey($_.Id)) { $d = ($_.CPU - $before[$_.Id]) / $lp * 100 }
  [pscustomobject]@{ name = $_.ProcessName; cpu = $d; mem = $_.WorkingSet64 }
}
$apps = $procs | Group-Object name | ForEach-Object {
  [pscustomobject]@{ name = $_.Name; count = $_.Count
    cpu = [math]::Round(($_.Group | Measure-Object cpu -Sum).Sum, 1)
    mem = [int64]($_.Group | Measure-Object mem -Sum).Sum }
}
$bat = Get-CimInstance Win32_Battery | Select-Object -First 1
$av = @(Get-CimInstance -Namespace root/SecurityCenter2 AntiVirusProduct -ErrorAction SilentlyContinue |
  ForEach-Object { [pscustomobject]@{ name = $_.displayName; on = ((($_.productState -shr 12) -band 0xF) -eq 1) } })
[pscustomobject]@{
  machine  = "$($cs.Manufacturer) $($cs.Model)"
  cpuName  = $cpu.Name.Trim()
  cpu      = [math]::Min(100, [math]::Round(($procs | Measure-Object cpu -Sum).Sum, 1))
  ramTotal = [int64]$os.TotalVisibleMemorySize * 1024
  ramFree  = [int64]$os.FreePhysicalMemory * 1024
  uptimeH  = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
  battery  = if ($bat) { [pscustomobject]@{ pct = $bat.EstimatedChargeRemaining; plugged = ($bat.BatteryStatus -eq 2) } } else { $null }
  drives   = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object { [pscustomobject]@{ id = $_.DeviceID; size = [int64]$_.Size; free = [int64]$_.FreeSpace } })
  disks    = @(Get-PhysicalDisk | ForEach-Object { [pscustomobject]@{ name = $_.FriendlyName; health = "$($_.HealthStatus)" } })
  antivirus = $av
  topCpu   = @($apps | Sort-Object cpu -Descending | Select-Object -First 8)
  topMem   = @($apps | Sort-Object mem -Descending | Select-Object -First 8)
} | ConvertTo-Json -Depth 4 -Compress
"""


def issues(h):
    """Plain-language list of things worth acting on, worst first."""
    out = []
    used = 1 - h["ramFree"] / h["ramTotal"]
    if used > 0.80:
        top = h["topMem"][0]
        out.append(("bad" if used > 0.92 else "warn",
                    f"Memory is {used:.0%} full. {top['name']} uses {top['mem'] / 2**30:.1f} GB.",
                    "Close apps or browser tabs you are not using."))
    if h["cpu"] > 80:
        out.append(("warn", f"CPU is {h['cpu']:.0f}% busy. {h['topCpu'][0]['name']} is the busiest app.", ""))
    for d in h["drives"]:
        free = d["free"] / d["size"]
        if free < 0.10:
            out.append(("bad" if free < 0.05 else "warn",
                        f"Drive {d['id']} has only {d['free'] / 2**30:.0f} GB free.", "Run a cleanup below."))
    for d in h["disks"]:
        if d["health"] != "Healthy":
            out.append(("bad", f"Disk {d['name']} reports {d['health']}.", "Back up your files now."))
    on = [a["name"] for a in h["antivirus"] if a["on"]]
    if len(on) > 1:
        out.append(("warn", f"{len(on)} antivirus programs are active: {', '.join(on)}.",
                    "Keep one. Two scanners slow every file read."))
    if h["uptimeH"] > 24 * 7:
        out.append(("warn", f"PC has not restarted in {h['uptimeH'] / 24:.0f} days.", "Restart to clear leaks and finish updates."))
    if h["battery"] and h["battery"]["pct"] < 20 and not h["battery"]["plugged"]:
        out.append(("warn", f"Battery is at {h['battery']['pct']}%.", "Plug in."))
    return [{"level": l, "text": t, "hint": hint} for l, t, hint in sorted(out, key=lambda i: i[0] != "bad")]


def health():
    h = ps(HEALTH_PS)
    h["issues"] = issues(h)
    return h


# ---------- Space ----------

TARGETS = {
    "user_temp": ("Your temp files", "Leftovers from installers and apps.", [os.environ["TEMP"]]),
    "win_temp": ("Windows temp files", "Some files need admin rights and get skipped.", [r"C:\Windows\Temp"]),
    "crash": ("Crash dumps and error reports", "Only useful when reporting a crash.",
              [LOCAL + r"\CrashDumps", r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive",
               r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue"]),
    "recycle": ("Recycle Bin", "Emptying it deletes these files for good.", []),
}


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def recycle_size():
    return ps("$s=0; (New-Object -ComObject Shell.Application).NameSpace(10).Items() | "
              "ForEach-Object { $s += $_.Size }; $s") or 0


def space():
    items = [{"id": k, "name": n, "note": note,
              "size": recycle_size() if k == "recycle" else sum(dir_size(p) for p in paths)}
             for k, (n, note, paths) in TARGETS.items()]
    return {"items": items, "downloads": dir_size(HOME / "Downloads")}


def clean_dir(path, cutoff):
    """Delete files older than cutoff under path; keep path itself. Never follows links."""
    freed = skipped = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0, 0
    for e in entries:
        try:
            if e.is_symlink() or os.path.isjunction(e.path):
                continue  # a link could point anywhere on disk
            if e.is_dir(follow_symlinks=False):
                f, s = clean_dir(e.path, cutoff)
                freed, skipped = freed + f, skipped + s
                try:
                    os.rmdir(e.path)  # only succeeds once empty
                except OSError:
                    pass
            else:
                st = e.stat(follow_symlinks=False)
                if st.st_mtime < cutoff:
                    os.unlink(e.path)
                    freed += st.st_size
                else:
                    skipped += 1
        except OSError:
            skipped += 1  # in use or no permission
    return freed, skipped


def clean(ids):
    freed = skipped = 0
    for k in ids:
        if k not in TARGETS:
            continue
        if k == "recycle":
            before = recycle_size()
            ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
            freed += before - recycle_size()
            continue
        for p in TARGETS[k][2]:
            f, s = clean_dir(p, time.time() - MIN_AGE)
            freed, skipped = freed + f, skipped + s
    return {"freed": freed, "skipped": skipped}


# ---------- Startup apps ----------

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
HIVES = {"user": winreg.HKEY_CURRENT_USER, "machine": winreg.HKEY_LOCAL_MACHINE}


def reg_values(hive, path):
    out = {}
    try:
        with winreg.OpenKey(hive, path) as k:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    return out
                out[name] = val
                i += 1
    except OSError:
        return out


def startup():
    rows = []
    for scope, hive in HIVES.items():
        approved = reg_values(hive, APPROVED)
        for name, cmd in reg_values(hive, RUN).items():
            flag = approved.get(name, b"\x02")
            rows.append({"scope": scope, "name": name, "command": cmd, "enabled": flag[0] % 2 == 0})
    return sorted(rows, key=lambda r: r["name"].lower())


def set_startup(scope, name, enabled):
    hive = HIVES[scope]
    if name not in reg_values(hive, RUN):
        raise ValueError(f"No startup entry named {name}")
    # Same format Task Manager writes: 02 = on, 03 = off, then a FILETIME.
    filetime = int((time.time() + 11644473600) * 1e7) if not enabled else 0
    data = bytes([2 if enabled else 3, 0, 0, 0]) + filetime.to_bytes(8, "little")
    try:
        with winreg.CreateKeyEx(hive, APPROVED, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, data)
    except PermissionError:
        raise ValueError("This one is set for all users. Run PC Hub as administrator to change it.")
    return {"ok": True}


# ---------- Tools ----------

TOOLS = {
    "taskmgr": "taskmgr.exe",
    "storage": "ms-settings:storagesense",
    "apps": "ms-settings:appsfeatures",
    "update": "ms-settings:windowsupdate",
    "security": "windowsdefender:",
    "cleanmgr": "cleanmgr.exe",
    "resmon": "resmon.exe",
    "devices": "devmgmt.msc",
    "downloads": str(HOME / "Downloads"),
}


def launch(tool):
    os.startfile(TOOLS[tool])
    return {"ok": True}


# ---------- Server ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local_host(self):
        # Blocks DNS-rebinding: only our own address may talk to us.
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def authed(self):
        return self.local_host() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def run(self, fn, *args):
        try:
            self.reply(200, fn(*args))
        except (ValueError, KeyError) as e:
            self.reply(400, {"error": str(e)})
        except Exception as e:
            self.reply(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        if self.path == "/" and self.local_host():
            html = (HERE / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            return self.reply(200, html.encode(), "text/html; charset=utf-8")
        routes = {"/api/health": health, "/api/space": space, "/api/startup": startup}
        if self.path not in routes or not self.authed():
            return self.reply(404 if self.authed() else 403, {"error": "not allowed"})
        self.run(routes[self.path])

    def do_POST(self):
        if not self.authed():
            return self.reply(403, {"error": "not allowed"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/clean":
            return self.run(clean, list(body.get("ids", [])))
        if self.path == "/api/startup":
            return self.run(set_startup, body["scope"], body["name"], bool(body["enabled"]))
        if self.path == "/api/launch":
            return self.run(launch, body["tool"])
        if self.path == "/api/quit":
            self.reply(200, {"ok": True})
            return threading.Thread(target=self.server.shutdown).start()
        self.reply(404, {"error": "not found"})


def selftest():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "sub").mkdir()
        old, new, nested = root / "old.tmp", root / "new.tmp", root / "sub" / "old2.tmp"
        for f in (old, new, nested):
            f.write_bytes(b"x" * 10)
        past = time.time() - 2 * MIN_AGE
        os.utime(old, (past, past))
        os.utime(nested, (past, past))
        outside = root.parent / f"pchub-keep-{os.getpid()}"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"k")
        os.utime(outside / "keep.txt", (past, past))
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)], capture_output=True)
        try:
            freed, skipped = clean_dir(root, time.time() - MIN_AGE)
            assert freed == 20 and skipped == 1, (freed, skipped)
            assert not old.exists() and not (root / "sub").exists() and new.exists()
            assert (outside / "keep.txt").exists(), "followed a junction"
        finally:
            os.unlink(outside / "keep.txt")
            outside.rmdir()
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    url = f"http://127.0.0.1:{PORT}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # already running
        webbrowser.open(url)
        sys.exit()
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    server.serve_forever()
