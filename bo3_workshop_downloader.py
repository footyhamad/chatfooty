#!/usr/bin/env python3
"""BO3 Workshop Downloader: a resilient SteamCMD wrapper for BO3 Workshop items."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, BooleanVar, StringVar, Tk, Menu, filedialog, messagebox
from tkinter import ttk

APP_ID = "311210"
STEAM_API = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
DEFAULTS = {
    "steamcmd": "",
    "steam_user": "",
    "output_dir": "BO3-Workshop",
    "max_retries": 0,
    "watchdog_seconds": 420,
    "stall_seconds": 75,
    "poll_seconds": 2.0,
    "auto_export": True,
    "inherit_steam_region": True,
    "retry_backoff_seconds": 5,
    "quick_failure_limit": 3,
    "quick_failure_window": 20,
    "last_started_ids": "3296316642",
    "dark_mode": False,
}
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SUCCESS_RE = re.compile(r"Success\. Downloaded item (\d+)", re.I)
LOGIN_FAIL_RE = re.compile(r"Cached credentials not found|Invalid Password|Login Failure|Not logged on|password:\s*$", re.I | re.M)
FAIL_RE = re.compile(r"ERROR!.*(?:Timeout|Failure)|Timeout downloading item|failed \(Failure\)", re.I)


def human_bytes(n: int | float) -> str:
    n = max(0.0, float(n))
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}" if i else f"{int(n)} B"


def human_speed(n: float) -> str:
    return human_bytes(n) + "/s"


def normalize_workshop_id(value: str) -> str:
    """Accept a numeric Workshop ID or a Steam Workshop URL."""
    value = value.strip().strip('"')
    if value.isdigit():
        return value
    match = re.search(r"(?:[?&]id=|/sharedfiles/filedetails/\?id=)(\d+)", value, re.I)
    return match.group(1) if match else ""


def eta_text(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds > 7 * 86400:
        return "--:--"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def sum_tree_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                pass
    return total


def system_network_bytes() -> int | None:
    """Return cumulative system network traffic in bytes.

    This is intentionally system-wide rather than pretending we can isolate
    SteamCMD traffic without packet-level/process tracing. On Windows we use
    Get-NetAdapterStatistics when available; psutil is used as a lightweight
    fallback if it is already installed. The value is only used for a live
    speed estimate, not for download accounting.
    """
    try:
        import psutil  # type: ignore
        counters = psutil.net_io_counters()
        if counters:
            return int(counters.bytes_recv + counters.bytes_sent)
    except Exception:
        pass

    if os.name == "nt":
        try:
            command = (
                "$s=Get-NetAdapterStatistics -ErrorAction Stop; "
                "$r=($s|Measure-Object -Property ReceivedBytes -Sum).Sum; "
                "$t=($s|Measure-Object -Property SentBytes -Sum).Sum; "
                "[Console]::WriteLine([int64]($r+$t))"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=4,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            match = re.search(r"(?m)^\s*(\d+)\s*$", result.stdout or "")
            if match:
                return int(match.group(1))
        except Exception:
            pass

    # Linux fallback for future portability.
    try:
        total = 0
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                values = line.split(":", 1)[1].split()
                if len(values) >= 9:
                    total += int(values[0]) + int(values[8])
        return total
    except Exception:
        return None


def network_connection_signature() -> str | None:
    """Return a small signature for the active network connection.

    On Windows, Wi-Fi BSSID/SSID changes are useful for detecting an access-point
    switch (for example 5 GHz -> 2.4 GHz). The downloader uses this only to
    restart SteamCMD cleanly; partial Workshop data is preserved.
    """
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            text = result.stdout or ""
            state = re.search(r"^\s*State\s*:\s*(.+)$", text, re.I | re.M)
            if state and state.group(1).strip().lower() == "connected":
                ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", text, re.I | re.M)
                bssid = re.search(r"^\s*BSSID\s*:\s*(.+)$", text, re.I | re.M)
                channel = re.search(r"^\s*Channel\s*:\s*(.+)$", text, re.I | re.M)
                return "wifi|" + "|".join([
                    ssid.group(1).strip() if ssid else "",
                    bssid.group(1).strip().lower() if bssid else "",
                    channel.group(1).strip() if channel else "",
                ])
        except Exception:
            pass

    # Fallback: identify active non-loopback IPv4 addresses. This also works
    # when the connection is Ethernet or when Wi-Fi details are unavailable.
    try:
        import psutil  # type: ignore
        parts = []
        for name, addrs in psutil.net_if_addrs().items():
            stats = psutil.net_if_stats().get(name)
            if not stats or not stats.isup:
                continue
            ipv4 = sorted(
                a.address for a in addrs
                if getattr(a, "family", None) == __import__("socket").AF_INET
                and not a.address.startswith("127.")
            )
            if ipv4:
                parts.append(name + "=" + ",".join(ipv4))
        if parts:
            return "ip|" + "|".join(sorted(parts))
    except Exception:
        pass
    return None


def detect_steam_user(steamcmd: Path) -> str:
    cfg = steamcmd.parent / "config" / "config.vdf"
    if not cfg.exists():
        return ""
    txt = cfg.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'"Accounts"\s*\{\s*"([^"]+)"', txt)
    return m.group(1) if m else ""


def find_steamcmd() -> Path | None:
    candidates = [Path(f"{d}:\\scmd\\steamcmd.exe") for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    candidates += [
        Path(r"C:\steamcmd\steamcmd.exe"),
        Path(r"D:\steamcmd\steamcmd.exe"),
        Path(r"C:\Program Files (x86)\SteamCMD\steamcmd.exe"),
        Path(r"C:\Program Files\SteamCMD\steamcmd.exe"),
    ]
    return next((p for p in candidates if p.exists()), None)

def find_steam_client_config() -> Path | None:
    """Find the normal Steam client's config without reading account credentials."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Steam" / "config" / "config.vdf",
        Path(os.environ.get("PROGRAMFILES", "")) / "Steam" / "config" / "config.vdf",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Steam" / "config" / "config.vdf",
    ]
    return next((p for p in candidates if p.is_file()), None)


def read_steam_cell_id(config: Path) -> str | None:
    """Read only Steam's selected download CellID."""
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"CellIDServerOverride"\s+"(\d+)"', text)
    if m:
        return m.group(1)
    m = re.search(r'"CurrentCellID"\s+"(\d+)"', text)
    return m.group(1) if m else None


@dataclass
class ItemInfo:
    item_id: str
    title: str = ""
    size: int = 0
    app: str = ""


class WorkshopAPI:
    @staticmethod
    def _page_size(item_id: str) -> int:
        # Steam's API can report file_size=0; use the public Workshop page as a fallback.
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            html = response.read().decode("utf-8", "replace")
        match = re.search(
            r"detailsStatLeft[^>]*>\s*File Size\s*</div>.*?"
            r"detailsStatRight[^>]*>\s*([^<]+)",
            html,
            re.I | re.S,
        )
        if not match:
            return 0
        text = re.sub(r"\s+", " ", match.group(1)).strip().replace(",", "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)", text, re.I)
        if not match:
            return 0
        value = float(match.group(1))
        multiplier = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[match.group(2).upper()]
        return int(value * multiplier)

    @staticmethod
    def get(item_id: str) -> ItemInfo:
        body = urllib.parse.urlencode({
            "itemcount": "1",
            "publishedfileids[0]": item_id,
        }).encode()
        req = urllib.request.Request(
            STEAM_API,
            data=body,
            headers={"User-Agent": "BO3-Workshop-Downloader/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)
        entry = data["response"]["publishedfiledetails"][0]
        if str(entry.get("result", "1")) != "1":
            raise RuntimeError(f"Steam returned result {entry.get('result')}")
        size = int(entry.get("file_size", 0) or 0)
        if not size:
            try:
                size = WorkshopAPI._page_size(item_id)
            except Exception:
                pass

        return ItemInfo(
            item_id=item_id,
            title=entry.get("title", "") or "",
            size=size,
            app=str(entry.get("consumer_app_id", "") or ""),
        )


class Engine:
    def __init__(self, steamcmd: Path, user: str, out: Path, *,
                 retries: int, watchdog: int, stall_seconds: int,
                 poll: float, auto_export: bool, retry_backoff_seconds: int, emit):
        self.steamcmd = steamcmd
        self.user = user
        self.out = out
        self.max_retries = max(0, retries)
        self.watchdog = max(120, watchdog)
        self.stall_seconds = max(30, stall_seconds)
        self.poll = max(0.5, poll)
        self.auto_export = auto_export
        self.retry_backoff_seconds = max(1, retry_backoff_seconds)
        self.quick_failure_limit = 3
        self.quick_failure_window = 20
        self.emit = emit
        self.stop_event = threading.Event()
        root = steamcmd.parent
        self.partial_root = root / "steamapps" / "workshop" / "downloads" / APP_ID
        self.installed_root = root / "steamapps" / "workshop" / "content" / APP_ID
        self.log_root = out / "_logs"
        self.content_log = root / "logs" / "content_log.txt"

    def sync_steam_region(self) -> str | None:
        """Mirror the normal Steam client's selected CDN region into SteamCMD."""
        client_cfg = find_steam_client_config()
        if not client_cfg:
            self.emit("log", "Steam region sync: normal Steam config.vdf not found")
            return None
        cell_id = read_steam_cell_id(client_cfg)
        if not cell_id:
            self.emit("log", f"Steam region sync: no CellID found in {client_cfg}")
            return None

        cmd_cfg = self.steamcmd.parent / "config" / "config.vdf"
        if not cmd_cfg.is_file():
            self.emit("log", f"Steam region sync: SteamCMD config not found at {cmd_cfg}")
            return None

        try:
            text = cmd_cfg.read_text(encoding="utf-8", errors="replace")
            backup = cmd_cfg.with_name("config.vdf.bo3wd-region-backup")
            if not backup.exists():
                shutil.copy2(cmd_cfg, backup)

            changed = False
            text, n1 = re.subn(
                r'("CellIDServerOverride"\s+")\d+(")',
                r'\g<1>' + cell_id + r'\g<2>',
                text, count=1,
            )
            text, n2 = re.subn(
                r'("CurrentCellID"\s+")\d+(")',
                r'\g<1>' + cell_id + r'\g<2>',
                text, count=1,
            )
            changed = bool(n1 or n2)
            if not n1:
                text, added = re.subn(
                    r'("Steam"\s*\{)',
                    r'\g<1>\n\t\t"CellIDServerOverride"\t"' + cell_id + r'"',
                    text, count=1,
                )
                changed = changed or bool(added)

            if changed:
                cmd_cfg.write_text(text, encoding="utf-8")
            self.emit("log", f"Steam region sync: using CellID {cell_id}" + (" (updated SteamCMD config)" if changed else ""))
            return cell_id
        except OSError as exc:
            self.emit("log", f"Steam region sync skipped: {exc}")
            return None

    def stop(self):
        self.stop_event.set()

    def clear_item_data(self, item_id: str) -> int:
        # A Workshop download has two SteamCMD-side locations: the partial
        # download cache and the installed Workshop copy. Clear BOTH so
        # SteamCMD cannot silently resume the old bytes.
        removed = 0
        for root in (self.partial_root, self.installed_root):
            p = root / item_id
            if p.exists():
                shutil.rmtree(p)
                if p.exists():
                    raise RuntimeError(f"SteamCMD data could not be fully removed: {p}")
                removed += 1
                self.emit("log", f"Cleared SteamCMD Workshop data: {p}")

        # Also remove our exported/ported copies for this exact Workshop ID.
        # This includes an interrupted ".partial" export from an older run.
        # Only folders ending in [item_id] or [item_id].partial are touched;
        # unrelated exports remain untouched.
        if self.out.is_dir():
            suffix = f" [{item_id}]"
            partial_suffix = f" [{item_id}].partial"
            for p in self.out.iterdir():
                if p.is_dir() and (p.name.endswith(suffix) or p.name.endswith(partial_suffix)):
                    shutil.rmtree(p)
                    if p.exists():
                        raise RuntimeError(f"Exported Workshop data could not be fully removed: {p}")
                    removed += 1
                    self.emit("log", f"Cleared old exported/ported copy: {p}")

        return removed

    def _cmd(self, item_id: str) -> list[str]:
        return [
            str(self.steamcmd),
            "+login", self.user,
            "+workshop_download_item", APP_ID, item_id,
            "validate", "+quit",
        ]

    def _log(self, item_id: str, line: str):
        self.log_root.mkdir(parents=True, exist_ok=True)
        with (self.log_root / f"{item_id}.log").open("a", encoding="utf-8", errors="replace") as f:
            f.write(line.rstrip() + "\n")

    def _content_log_tail(self, max_bytes: int = 32768) -> str:
        """Read SteamCMD's content log tail without disturbing SteamCMD."""
        try:
            with self.content_log.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes), os.SEEK_SET)
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def _steamcmd_failure_signal(self, text: str) -> str | None:
        """Detect the failure forms SteamCMD commonly emits."""
        if not text:
            return None
        patterns = (
            r"ERROR!.*Download item.*failed",
            r"Timeout downloading item",
            r"failed \(Failure\)",
            r"ERROR!.*(?:Timeout|Failure)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if match:
                return re.sub(r"\s+", " ", match.group(0)).strip()[-240:]
        return None

    def _progress(self, item: ItemInfo, attempt: int, started: float,
                  prev_b: int, prev_t: float, last_net_b: int | None,
                  last_net_t: float, net_samples: list[tuple[float, int]],
                  network_speed_ema: float):
        partial = self.partial_root / item.item_id
        installed = self.installed_root / item.item_id
        current = max(sum_tree_bytes(partial), sum_tree_bytes(installed))
        now = time.monotonic()
        dt = max(0.5, now - prev_t)
        db = max(0, current - prev_b)

        # SteamCMD writes data in bursts, so filesystem growth is useful as a
        # diagnostic but is NOT the network download rate.
        disk_speed = db / dt if db > 0 else 0.0

        # PowerShell is only a fallback for machines without psutil and is
        # relatively expensive to launch. Sample the system counter every 3s.
        # This keeps the watchdog/progress loop responsive.
        net_now = last_net_b
        if now - last_net_t >= 3.0:
            sampled = system_network_bytes()
            last_net_t = now
            if sampled is not None:
                net_now = sampled
                if last_net_b is not None and sampled >= last_net_b:
                    net_samples.append((now, sampled))
                elif last_net_b is not None and sampled < last_net_b:
                    # Adapter/counter reset (common after switching Wi-Fi).
                    # Start a fresh measurement window instead of producing a
                    # bogus multi-second speed spike or zero-rate ETA.
                    net_samples.clear()
                    network_speed_ema = 0.0
                last_net_b = sampled

        cutoff = now - 8.0
        net_samples[:] = [(t, b) for t, b in net_samples if t >= cutoff]
        if len(net_samples) >= 2:
            t0, b0 = net_samples[0]
            elapsed = max(0.5, now - t0)
            raw_network_speed = max(0.0, (net_samples[-1][1] - b0) / elapsed)
            # Smooth the displayed rate so normal 5G/Wi-Fi bursts do not make
            # the UI jump wildly. This does not alter download accounting.
            if network_speed_ema <= 0:
                network_speed = raw_network_speed
            else:
                network_speed = (network_speed_ema * 0.75) + (raw_network_speed * 0.25)
            network_speed_ema = network_speed
        else:
            network_speed = network_speed_ema if network_speed_ema > 0 else 0.0

        # System network traffic is only a diagnostic. Use actual Workshop
        # file growth for the ETA so unrelated downloads cannot make it lie.
        eta_speed = disk_speed
        pct = (current / item.size * 100.0) if item.size else None
        eta = ((item.size - current) / eta_speed) if item.size and eta_speed > 0 else None
        self.emit("progress", {
            "bytes": current,
            "total": item.size,
            "pct": pct,
            "speed": network_speed,
            "disk_speed": disk_speed,
            "eta": eta,
            "attempt": attempt,
            "elapsed": now - started,
            "network_available": last_net_b is not None,
        })
        return current, now, last_net_b, last_net_t, network_speed

    def verify_completed(self, item: ItemInfo, installed: Path) -> tuple[bool, str]:
        """Perform cheap post-download integrity checks before declaring success."""
        if not installed.is_dir():
            return False, "SteamCMD reported success, but the installed Workshop directory is missing."

        size = sum_tree_bytes(installed)
        if size <= 0:
            return False, "SteamCMD reported success, but the installed Workshop directory is empty."

        file_count = 0
        try:
            for _base, _dirs, files in os.walk(installed):
                file_count += len(files)
        except OSError as exc:
            return False, f"Could not inspect the installed Workshop directory: {exc}"

        if file_count == 0:
            return False, "SteamCMD reported success, but no installed Workshop files were found."

        if item.size:
            ratio = size / item.size
            if ratio < 0.95 or ratio > 1.05:
                # Do not silently reject unusual Workshop metadata. The SteamCMD
                # success marker remains authoritative, but surface a clear warning.
                self.emit(
                    "log",
                    f"Integrity warning: installed size {human_bytes(size)} differs "
                    f"from Steam's reported {human_bytes(item.size)} ({ratio * 100:.1f}%)."
                )

        return True, f"Verified {file_count:,} file(s), {human_bytes(size)} on disk"

    def export(self, item: ItemInfo) -> Path:
        src = self.installed_root / item.item_id
        if not src.is_dir():
            raise RuntimeError("SteamCMD reported success but the installed Workshop directory is missing")
        safe = re.sub(r'[^A-Za-z0-9._ -]+', "_", item.title or item.item_id).strip(" .") or item.item_id
        dest = self.out / f"{safe} [{item.item_id}]"
        tmp = self.out / f".{safe} [{item.item_id}].partial"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        self.out.mkdir(parents=True, exist_ok=True)
        entries: list[tuple[Path, Path, int]] = []
        total = 0
        for base, _dirs, names in os.walk(src):
            for name in names:
                src_file = Path(base) / name
                try:
                    size = src_file.stat().st_size
                except OSError:
                    continue
                total += size
                entries.append((src_file, src_file.relative_to(src), size))
        copied = 0
        for src_file, rel, size in entries:
            if self.stop_event.is_set():
                raise RuntimeError("Stopped during export")
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)
            copied += size
            self.emit("export_progress", (copied, total, dest.name))
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        tmp.replace(dest)

        exported_size = sum_tree_bytes(dest)
        if exported_size != total:
            raise RuntimeError(
                f"Export verification failed: source={human_bytes(total)}, "
                f"export={human_bytes(exported_size)}"
            )
        return dest

    def _kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=False,
                )
            else:
                proc.kill()
        except OSError:
            pass

    def download(self, item: ItemInfo, *, require_empty_start: bool = False) -> Path | None:
        attempt = 0
        prev_b = max(
            sum_tree_bytes(self.partial_root / item.item_id),
            sum_tree_bytes(self.installed_root / item.item_id),
        )
        prev_t = time.monotonic()
        last_net_b = system_network_bytes()
        last_net_t = prev_t
        net_samples: list[tuple[float, int]] = []
        network_speed_ema = 0.0
        network_signature = network_connection_signature()
        last_network_check = prev_t
        last_activity = prev_t
        observed_b = prev_b

        # A FRESH DOWNLOAD must really begin at zero. Check once before
        # SteamCMD starts; after that, partial bytes are intentionally kept
        # so timeout retries can resume safely.
        if require_empty_start and prev_b != 0:
            self.emit("finished", (
                False,
                "Fresh download refused: old Workshop bytes are still present "
                f"({human_bytes(prev_b)}). Clear the item again before starting."
            ))
            self.emit("log", f"Fresh-start check failed: partial={self.partial_root / item.item_id}")
            self.emit("log", f"Fresh-start check failed: installed={self.installed_root / item.item_id}")
            return None
        if require_empty_start:
            self.emit("log", "Fresh-start check passed: SteamCMD Workshop byte count is 0 B")

        while not self.stop_event.is_set():
            attempt += 1
            if self.max_retries and attempt > self.max_retries:
                self.emit("finished", (False, f"Retry limit reached: {self.max_retries}"))
                return None

            partial = self.partial_root / item.item_id
            self.emit("status", f"Attempt {attempt}: " +
                      ("resuming existing partial data" if partial.exists() else "starting"))
            self._log(item.item_id, f"\\n=== attempt {attempt} ===")
            self._log(item.item_id, "$ steamcmd +login *** +workshop_download_item 311210 " + item.item_id + " validate +quit")

            proc = subprocess.Popen(
                self._cmd(item.item_id),
                cwd=str(self.steamcmd.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            start = time.monotonic()
            output: list[str] = []
            q: queue.Queue[str | None] = queue.Queue()

            def reader():
                try:
                    if proc.stdout:
                        for line in proc.stdout:
                            q.put(line)
                finally:
                    q.put(None)

            threading.Thread(target=reader, daemon=True).start()
            last_scan = 0.0
            killed = False
            killed_reason = ""
            stalled = False
            last_activity = time.monotonic()
            content_log = self.steamcmd.parent / "logs" / "content_log.txt"
            try:
                content_log_stamp = (content_log.stat().st_mtime_ns, content_log.stat().st_size)
            except OSError:
                content_log_stamp = None
            network_signature = network_connection_signature()
            last_network_check = last_activity
            observed_b = max(
                sum_tree_bytes(self.partial_root / item.item_id),
                sum_tree_bytes(self.installed_root / item.item_id),
            )

            while proc.poll() is None and not self.stop_event.is_set():
                while True:
                    try:
                        line = q.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        break
                    clean = ANSI_RE.sub("", line.rstrip())
                    output.append(clean)
                    self._log(item.item_id, clean)
                    self.emit("log", clean)
                    last_activity = time.monotonic()

                now = time.monotonic()
                if now - last_scan >= self.poll:
                    previous_b = observed_b
                    prev_b, prev_t, last_net_b, last_net_t, network_speed_ema = self._progress(
                        item, attempt, start, prev_b, prev_t,
                        last_net_b, last_net_t, net_samples, network_speed_ema,
                    )
                    if prev_b > previous_b:
                        observed_b = prev_b
                        last_activity = now

                    # SteamCMD writes detailed transfer and validation updates
                    # to content_log.txt even when its stdout is quiet.
                    try:
                        new_content_log_stamp = (
                            content_log.stat().st_mtime_ns,
                            content_log.stat().st_size,
                        )
                    except OSError:
                        new_content_log_stamp = None
                    if new_content_log_stamp != content_log_stamp:
                        content_log_stamp = new_content_log_stamp
                        last_activity = now
                        failure_signal = self._steamcmd_failure_signal(self._content_log_tail())
                        if failure_signal:
                            killed = True
                            killed_reason = "SteamCMD content-log failure"
                            self._kill(proc)
                            self.emit("status", "SteamCMD reported a download failure — restarting; partial data is preserved")
                            self._log(item.item_id, "CONTENT LOG FAILURE: " + failure_signal)
                            break

                    # If the active Wi-Fi connection changes while SteamCMD is
                    # running, restart SteamCMD instead of leaving the old
                    # session stuck on the previous network path. Partial data
                    # is preserved and the normal retry/backoff path takes over.
                    if now - last_network_check >= 2.0:
                        new_signature = network_connection_signature()
                        last_network_check = now
                        if network_signature is not None and new_signature != network_signature:
                            killed = True
                            self._kill(proc)
                            self.emit(
                                "status",
                                "Network connection changed — restarting SteamCMD; partial data is preserved",
                            )
                            self._log(item.item_id, "NETWORK CHANGE DETECTED: restarting SteamCMD")
                            killed_reason = "network change"
                            network_signature = new_signature
                            break
                        network_signature = new_signature

                    if now - last_activity >= self.stall_seconds:
                        stalled = True
                        killed = True
                        killed_reason = "stall"
                        self._kill(proc)
                        self.emit(
                            "status",
                            f"No download activity for {self.stall_seconds}s — restarting; partial data is preserved",
                        )
                        self._log(item.item_id, f"STALL DETECTED after {self.stall_seconds}s without SteamCMD activity")
                        break

                    # This is deliberately inactivity-based, not a hard cap on
                    # the total session duration. Slow, healthy downloads and
                    # validation passes may run longer than the watchdog value.
                    if now - last_activity >= self.watchdog:
                        killed = True
                        killed_reason = "watchdog"
                        self._kill(proc)
                        self.emit("status", f"No SteamCMD activity for {self.watchdog}s — restarting; partial data is preserved")
                        self._log(item.item_id, f"WATCHDOG KILL after {self.watchdog}s without activity")
                        break

                    last_scan = now
                time.sleep(0.05)

            if self.stop_event.is_set():
                self._kill(proc)
                self.emit("finished", (False, "Stopped; partial Workshop data was preserved"))
                return None

            try:
                if proc.stdout:
                    for line in proc.stdout:
                        clean = ANSI_RE.sub("", line.rstrip())
                        output.append(clean)
                        self._log(item.item_id, clean)
                        self.emit("log", clean)
            except Exception:
                pass

            text = "\n".join(output)
            final_b = max(
                sum_tree_bytes(self.partial_root / item.item_id),
                sum_tree_bytes(self.installed_root / item.item_id),
            )
            self.emit("progress", {
                "bytes": final_b,
                "total": item.size,
                "pct": min(100.0, final_b / item.size * 100.0) if item.size else None,
                "speed": 0.0,
                "disk_speed": 0.0,
                "eta": 0,
                "attempt": attempt,
                "elapsed": time.monotonic() - start,
                "network_available": last_net_b is not None,
            })

            if SUCCESS_RE.search(text):
                installed = self.installed_root / item.item_id
                verified, verification_msg = self.verify_completed(item, installed)
                if not verified:
                    self.emit("status", verification_msg + " Retrying without deleting data.")
                    self._log(item.item_id, "INTEGRITY CHECK FAILED: " + verification_msg)
                    got = max(
                        sum_tree_bytes(self.partial_root / item.item_id),
                        sum_tree_bytes(installed),
                    )
                    prev_b = got
                    prev_t = time.monotonic()
                    delay = min(60, self.retry_backoff_seconds * (2 ** min(attempt - 1, 4)))
                    self.emit("status", f"Retrying in {delay}s after integrity failure")
                    if self.stop_event.wait(delay):
                        break
                    continue

                self.emit("status", "Integrity check passed: " + verification_msg)
                try:
                    exported = self.export(item) if self.auto_export else installed
                    self.emit("finished", (True, f"Completed → {exported}"))
                    return installed
                except Exception as exc:
                    self.emit("finished", (False, f"Download finished, export failed: {exc}"))
                    return installed

            if LOGIN_FAIL_RE.search(text):
                self.emit("finished", (False, "SteamCMD login failed. Log in once manually with this SteamCMD install."))
                return None

            if killed or FAIL_RE.search(text) or proc.returncode not in (0, None):
                duration = time.monotonic() - start
                if duration < self.quick_failure_window:
                    quick_failures += 1
                else:
                    quick_failures = 0
                got = max(
                    sum_tree_bytes(self.partial_root / item.item_id),
                    sum_tree_bytes(self.installed_root / item.item_id),
                )
                reason = (
                    killed_reason if killed_reason else
                    "SteamCMD failure"
                )
                delay = min(60, self.retry_backoff_seconds * (2 ** min(attempt - 1, 4)))
                if quick_failures >= self.quick_failure_limit:
                    delay = max(delay, 60)
                    self.emit(
                        "status",
                        f"SteamCMD failed {quick_failures} times in under {self.quick_failure_window}s — cooling down for 60s",
                    )
                    self._log(item.item_id, f"ADAPTIVE COOLDOWN: quick_failures={quick_failures}")
                    quick_failures = 0
                self.emit(
                    "status",
                    f"{reason.capitalize()} at {human_bytes(got)} — retrying in {delay}s; "
                    "partial data will be preserved",
                )
                self._log(item.item_id, f"RETRY: reason={reason}, delay={delay}s, bytes={got}")
                prev_b = got
                prev_t = time.monotonic()
                if self.stop_event.wait(delay):
                    break
                continue

            delay = min(60, self.retry_backoff_seconds * (2 ** min(attempt - 1, 4)))
            self.emit("status", f"No success marker — retrying in {delay}s")
            self._log(item.item_id, f"RETRY: no success marker, delay={delay}s")
            prev_b = final_b
            prev_t = time.monotonic()
            if self.stop_event.wait(delay):
                break

        self.emit("finished", (False, "Stopped"))
        return None


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("BO3 Workshop Downloader")
        self.root.geometry("1020x760")
        self.root.minsize(900, 650)
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.engine: Engine | None = None
        # IDs cleared with FRESH DOWNLOAD are tracked until their first launch.
        # This lets START / RESUME verify that a fresh job really starts at 0 B.
        self.fresh_ids: set[str] = set()
        self.cfg_path = Path(__file__).with_name("bo3wd.json")
        cfg = {**DEFAULTS, **read_json(self.cfg_path)}
        self.light_theme = ttk.Style(self.root).theme_use()
        self.last_started_ids = str(cfg["last_started_ids"])

        detected = find_steamcmd()
        self.steamcmd_var = StringVar(value=cfg["steamcmd"] or (str(detected) if detected else ""))
        user = cfg["steam_user"] or (detect_steam_user(Path(self.steamcmd_var.get())) if self.steamcmd_var.get() else "")
        self.user_var = StringVar(value=user)
        self.output_var = StringVar(value=cfg["output_dir"])
        self.ids_var = StringVar(value=self.last_started_ids)
        self.retry_var = StringVar(value=str(cfg["max_retries"]))
        self.watchdog_var = StringVar(value=str(cfg["watchdog_seconds"]))
        self.stall_var = StringVar(value=str(cfg["stall_seconds"]))
        self.auto_export_var = BooleanVar(value=bool(cfg["auto_export"]))
        self.inherit_region_var = BooleanVar(value=bool(cfg["inherit_steam_region"]))
        self.dark_mode_var = BooleanVar(value=bool(cfg["dark_mode"]))

        self.status_var = StringVar(value="Ready")
        self.progress_var = StringVar(value="0 B / unknown")
        self.speed_var = StringVar(value="System network: 0 B/s")
        self.disk_speed_var = StringVar(value="Disk: 0 B/s")
        self.eta_var = StringVar(value="ETA --:--")
        self.attempt_var = StringVar(value="Attempt 0")
        self.info_var = StringVar(value="")

        self.build_ui()
        self.apply_theme()
        self.root.after(100, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=BOTH, expand=True)

        config = ttk.LabelFrame(main, text="SteamCMD", padding=8)
        config.pack(fill=X)
        ttk.Label(config, text="steamcmd.exe").grid(row=0, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(config, textvariable=self.steamcmd_var).grid(row=0, column=1, sticky="ew", padx=6, pady=5)
        ttk.Button(config, text="Browse", command=self.browse_steamcmd).grid(row=0, column=2, padx=6)
        ttk.Label(config, text="Steam account").grid(row=1, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(config, textvariable=self.user_var).grid(row=1, column=1, sticky="ew", padx=6, pady=5)
        ttk.Label(config, text="Output folder").grid(row=2, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(config, textvariable=self.output_var).grid(row=2, column=1, sticky="ew", padx=6, pady=5)
        ttk.Button(config, text="Browse", command=self.browse_output).grid(row=2, column=2, padx=6)
        self.update_btn = ttk.Button(config, text="UPDATE", command=self.update_app)
        self.update_btn.grid(row=0, column=3, rowspan=3, padx=(14, 6), sticky="ns")
        ttk.Checkbutton(
            config, text="Dark mode", variable=self.dark_mode_var, command=self.apply_theme,
        ).grid(row=3, column=1, sticky="w", padx=6, pady=(3, 5))
        config.columnconfigure(1, weight=1)

        workshop = ttk.LabelFrame(main, text="Workshop queue", padding=8)
        workshop.pack(fill=X, pady=(10, 0))
        ttk.Label(workshop, text="IDs / Workshop URLs").grid(row=0, column=0, sticky="w", padx=6, pady=5)
        ttk.Entry(workshop, textvariable=self.ids_var).grid(row=0, column=1, sticky="ew", padx=6, pady=5)
        ttk.Button(workshop, text="Lookup", command=self.lookup).grid(row=0, column=2, padx=6)
        self.start_btn = ttk.Button(workshop, text="START / RESUME", command=self.start)
        self.start_btn.grid(row=1, column=1, sticky="w", padx=6, pady=5)
        self.clear_btn = ttk.Button(workshop, text="FRESH DOWNLOAD", command=self.clear_old_data)
        self.clear_btn.grid(row=1, column=2, padx=6, pady=5)
        self.stop_btn = ttk.Button(workshop, text="STOP", command=self.stop, state="disabled")
        self.stop_btn.grid(row=1, column=1, sticky="e", padx=6, pady=5)
        ttk.Label(workshop, textvariable=self.info_var).grid(row=2, column=1, sticky="w", padx=6, pady=5)
        workshop.columnconfigure(1, weight=1)

        opts = ttk.Frame(main)
        opts.pack(fill=X, pady=(8, 0))
        ttk.Label(opts, text="Max retries (0 = infinite)").pack(side=LEFT, padx=5)
        ttk.Entry(opts, textvariable=self.retry_var, width=7).pack(side=LEFT)
        ttk.Label(opts, text="Watchdog (sec)").pack(side=LEFT, padx=(18, 5))
        ttk.Entry(opts, textvariable=self.watchdog_var, width=7).pack(side=LEFT)
        ttk.Label(opts, text="Stall (sec)").pack(side=LEFT, padx=(18, 5))
        ttk.Entry(opts, textvariable=self.stall_var, width=7).pack(side=LEFT)
        ttk.Checkbutton(opts, text="Export completed item", variable=self.auto_export_var).pack(side=LEFT, padx=18)
        ttk.Checkbutton(opts, text="Use Steam client's download region", variable=self.inherit_region_var).pack(side=LEFT, padx=18)

        progress = ttk.LabelFrame(main, text="Real progress", padding=10)
        progress.pack(fill=X, pady=(10, 0))
        self.bar = ttk.Progressbar(progress, maximum=100, mode="determinate")
        self.bar.pack(fill=X, pady=(0, 8))
        row = ttk.Frame(progress)
        row.pack(fill=X)
        ttk.Label(row, textvariable=self.progress_var, font=("Segoe UI", 11, "bold")).pack(side=LEFT)
        ttk.Label(row, textvariable=self.speed_var).pack(side=LEFT, padx=25)
        ttk.Label(row, textvariable=self.disk_speed_var).pack(side=LEFT, padx=25)
        ttk.Label(row, textvariable=self.eta_var).pack(side=LEFT, padx=25)
        ttk.Label(row, textvariable=self.attempt_var).pack(side=RIGHT)

        ttk.Label(main, textvariable=self.status_var).pack(fill=X, pady=(8, 4))

        logs = ttk.LabelFrame(main, text="SteamCMD output", padding=5)
        logs.pack(fill=BOTH, expand=True)
        self.log_box = ttk.Treeview(logs, columns=("line",), show="headings", selectmode="extended")
        self.log_box.heading("line", text="Output")
        self.log_box.column("line", width=900)
        self.log_box.bind("<Control-c>", self.copy_log)
        self.log_box.bind("<Control-a>", self.select_all_log)
        self.log_box.bind("<Button-3>", self.log_context_menu)
        scroll = ttk.Scrollbar(logs, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)

        self.log_menu = Menu(self.root, tearoff=False)
        self.log_menu.add_command(label="Copy", command=self.copy_log)
        self.log_menu.add_command(label="Select All", command=self.select_all_log)

    def update_app(self):
        """Download the latest source from GitHub and replace this file.
        
        This is deliberately NOT a git operation. It works from a copied source folder
        as well as from a checkout. Private-repository access uses the authenticated
        GitHub CLI when available; public repositories can use the raw URL directly.
        """
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Update", "Stop the current download before updating.")
            return

        self.save()
        self.update_btn.configure(state="disabled")
        self.status_var.set("Checking GitHub for updates…")

        def work():
            try:
                script = Path(__file__).resolve()
                current_bytes = script.read_bytes()
                current_sha = hashlib.sha1(
                    b"blob " + str(len(current_bytes)).encode() + b"\0" + current_bytes
                ).hexdigest()

                remote_bytes, remote_sha = self.fetch_remote_source()
                if remote_sha == current_sha:
                    self.events.put(("update_result", (True, "Already up to date.")))
                    return

                if not remote_bytes.strip().startswith(b"#!"):
                    raise RuntimeError("GitHub returned unexpected data; update aborted")

                temp = script.with_suffix(script.suffix + ".update")
                temp.write_bytes(remote_bytes)
                self.events.put(("update_ready", str(temp)))
            except Exception as exc:
                self.events.put(("update_result", (False, str(exc))))

        self.update_thread = threading.Thread(target=work, daemon=True)
        self.update_thread.start()

    def fetch_remote_source(self) -> tuple[bytes, str]:
        """Fetch bo3_workshop_downloader.py from the main branch without requiring Git."""
        raw_url = "https://raw.githubusercontent.com/footyhamad/chatfooty/main/bo3_workshop_downloader.py"
        try:
            req = urllib.request.Request(
                raw_url,
                headers={"User-Agent": "BO3-Workshop-Downloader-Updater/1.0"},
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                data = response.read()
            sha = hashlib.sha1(
                b"blob " + str(len(data)).encode() + b"\0" + data
            ).hexdigest()
            return data, sha
        except Exception:
            # The repository is private in the normal setup, so use an already
            # authenticated GitHub CLI session as the fallback.
            try:
                proc = subprocess.run(
                    [
                        "gh", "api",
                        "repos/footyhamad/chatfooty/contents/bo3_workshop_downloader.py",
                        "--method", "GET",
                        "--field", "ref=main",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "GitHub update requires either a public repo or GitHub CLI (gh) "
                    "authenticated to the repo."
                ) from exc
            if proc.returncode != 0:
                detail = proc.stderr.decode("utf-8", "replace").strip()
                raise RuntimeError("GitHub API update failed: " + (detail or "unknown error"))

            payload = json.loads(proc.stdout.decode("utf-8"))
            data = base64.b64decode(payload["content"])
            remote_sha = payload.get("sha", "")
            if not remote_sha:
                remote_sha = hashlib.sha1(
                    b"blob " + str(len(data)).encode() + b"\0" + data
                ).hexdigest()
            return data, remote_sha

    def restart_after_update(self, temp_path: str):
        self.save()
        script = Path(__file__).resolve()
        temp = Path(temp_path)
        helper = script.with_name(".bo3wd_apply_update.cmd")
        py = str(Path(sys.executable).resolve())
        bat = (
            "@echo off\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            f'move /Y "{temp}" "{script}" >nul\r\n'
            f'start "" "{py}" "{script}"\r\n'
            'del "%~f0"\r\n'
        )
        try:
            helper.write_text(bat, encoding="utf-8")
            subprocess.Popen(
                ["cmd.exe", "/d", "/c", str(helper)],
                cwd=str(script.parent),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.root.destroy()
        except Exception as exc:
            messagebox.showerror("Update", f"Updated download was prepared, but restart failed:\n{exc}")

    def browse_steamcmd(self):
        p = filedialog.askopenfilename(filetypes=[("SteamCMD", "steamcmd.exe"), ("Executable", "*.exe")])
        if p:
            self.steamcmd_var.set(p)
            if not self.user_var.get():
                self.user_var.set(detect_steam_user(Path(p)))

    def browse_output(self):
        p = filedialog.askdirectory()
        if p:
            self.output_var.set(p)

    def apply_theme(self):
        style = ttk.Style(self.root)
        if not self.dark_mode_var.get():
            style.theme_use(self.light_theme)
            self.root.configure(background="SystemButtonFace")
            return

        style.theme_use("clam")
        background = "#1e1f22"
        surface = "#2b2d31"
        input_background = "#313338"
        foreground = "#f2f3f5"
        accent = "#5865f2"
        self.root.configure(background=background)
        style.configure(".", background=background, foreground=foreground)
        style.configure("TFrame", background=background)
        style.configure("TLabel", background=background, foreground=foreground)
        style.configure("TLabelframe", background=background, foreground=foreground)
        style.configure("TLabelframe.Label", background=background, foreground=foreground)
        style.configure("TEntry", fieldbackground=input_background, foreground=foreground)
        style.configure("TCheckbutton", background=background, foreground=foreground)
        style.configure("TButton", background=surface, foreground=foreground, padding=(8, 4))
        style.map("TButton", background=[("active", accent), ("pressed", "#4752c4")])
        style.configure("Treeview", background=input_background, fieldbackground=input_background, foreground=foreground)
        style.configure("Treeview.Heading", background=surface, foreground=foreground)
        style.map("Treeview", background=[("selected", accent)], foreground=[("selected", foreground)])
        style.configure("Horizontal.TProgressbar", troughcolor=input_background, background=accent)

    def ids(self) -> list[str]:
        values = re.split(r"[\s,;]+", self.ids_var.get())
        return list(dict.fromkeys(i for i in (normalize_workshop_id(x) for x in values) if i))

    def save(self):
        # Keep the persisted settings intentionally small and human-readable.
        write_json(self.cfg_path, {
            "steamcmd": self.steamcmd_var.get().strip(),
            "steam_user": self.user_var.get().strip(),
            "output_dir": self.output_var.get().strip(),
            "max_retries": int(self.retry_var.get() or 0),
            "watchdog_seconds": int(self.watchdog_var.get() or 420),
            "stall_seconds": int(self.stall_var.get() or 75),
            "poll_seconds": 2.0,
            "auto_export": bool(self.auto_export_var.get()),
            "inherit_steam_region": bool(self.inherit_region_var.get()),
            "last_started_ids": self.last_started_ids,
            "dark_mode": bool(self.dark_mode_var.get()),
        })

    def lookup(self):
        ids = self.ids()
        if not ids:
            messagebox.showerror("Workshop", "Enter a numeric Workshop ID.")
            return
        if self.inherit_region_var.get():
            steamcmd = Path(self.steamcmd_var.get().strip().strip('"'))
            if steamcmd.is_file():
                lookup_engine = Engine(
                    steamcmd, self.user_var.get().strip() or detect_steam_user(steamcmd),
                    Path(self.output_var.get().strip().strip('"') or "BO3-Workshop"),
                    retries=0, watchdog=420, stall_seconds=75, poll=2.0,
                    auto_export=False, retry_backoff_seconds=5,
                    emit=lambda kind, payload: self.events.put((kind, payload)),
                )
                lookup_engine.sync_steam_region()

        def work():
            for iid in ids:
                try:
                    info = WorkshopAPI.get(iid)
                    self.events.put(("info", f"{iid}: {info.title or 'unknown'} — {human_bytes(info.size) if info.size else 'size unknown'}"))
                except Exception as exc:
                    self.events.put(("log", f"Lookup {iid} failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        steamcmd = Path(self.steamcmd_var.get().strip().strip('"'))
        user = self.user_var.get().strip() or detect_steam_user(steamcmd)
        out = Path(self.output_var.get().strip().strip('"') or "BO3-Workshop")
        ids = self.ids()
        if not steamcmd.is_file():
            messagebox.showerror("SteamCMD", "Select the SteamCMD executable.")
            return
        if not user:
            messagebox.showerror("Steam login", "No cached login found. Log in once manually with this SteamCMD install.")
            return
        if not ids:
            messagebox.showerror("Workshop", "Enter at least one numeric Workshop ID.")
            return
        try:
            retries = int(self.retry_var.get())
            watchdog = int(self.watchdog_var.get())
            stall_seconds = int(self.stall_var.get())
            if stall_seconds < 30:
                raise ValueError
        except ValueError:
            messagebox.showerror("Settings", "Retries, watchdog, and stall timeout must be valid integers (stall >= 30).")
            return

        # Persist only IDs that have actually been started, rather than saving
        # transient edits that were never submitted to SteamCMD.
        self.last_started_ids = " ".join(ids)
        self.save()
        self.log_box.delete(*self.log_box.get_children())
        self.bar.configure(value=0)
        self.status_var.set("Preparing…")
        self.start_btn.configure(state="disabled")
        self.clear_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        self.engine = Engine(
            steamcmd, user, out,
            retries=retries, watchdog=watchdog,
            stall_seconds=stall_seconds,
            poll=2.0, auto_export=self.auto_export_var.get(),
            retry_backoff_seconds=5,
            emit=lambda kind, payload: self.events.put((kind, payload)),
        )

        if self.inherit_region_var.get():
            # SteamCMD has no documented region selector; mirror the normal
            # Steam client's CellID as a best-effort CDN-region preference.
            self.engine.sync_steam_region()

        def work():
            for iid in ids:
                if self.engine.stop_event.is_set():
                    break
                try:
                    info = WorkshopAPI.get(iid)
                    self.events.put(("info", f"{iid}: {info.title or 'unknown'} — {human_bytes(info.size) if info.size else 'size unknown'}"))
                except Exception as exc:
                    self.events.put(("log", f"Metadata lookup failed for {iid}: {exc}"))
                    info = ItemInfo(iid)
                if info.app and info.app != APP_ID:
                    self.events.put(("finished", (False, f"{iid} belongs to app {info.app}, not BO3 ({APP_ID})")))
                    continue
                self.events.put(("status", f"Downloading {iid}: {info.title or 'unknown'}"))
                is_fresh = iid in self.fresh_ids
                self.engine.download(info, require_empty_start=is_fresh)
                # After the first launch, a failed download is resumable by design.
                self.fresh_ids.discard(iid)
            self.events.put(("all_done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def clear_old_data(self):
        # This button intentionally affects only the Workshop IDs in the queue.
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Clear old data", "Stop the current download before clearing old data.")
            return
        ids = self.ids()
        if not ids:
            messagebox.showerror("Clear old data", "Enter at least one numeric Workshop ID.")
            return
        if not messagebox.askyesno(
            "Clear old data",
            "Delete ALL old download data for:\n\n" + ", ".join(ids) +
            "\n\nThis removes SteamCMD partial/installed data AND exported/ported copies for these IDs.",
            icon="warning",
        ):
            return

        steamcmd = Path(self.steamcmd_var.get().strip().strip('"'))
        if not steamcmd.is_file():
            messagebox.showerror("SteamCMD", "Select the SteamCMD executable first.")
            return

        user = self.user_var.get().strip() or detect_steam_user(steamcmd)
        out = Path(self.output_var.get().strip().strip('"') or "BO3-Workshop")
        engine = Engine(
            steamcmd, user, out,
            retries=0, watchdog=420, stall_seconds=75, poll=2.0,
            auto_export=False, retry_backoff_seconds=5,
            emit=lambda kind, payload: self.events.put((kind, payload)),
        )
        try:
            removed = sum(engine.clear_item_data(iid) for iid in ids)
            self.fresh_ids.update(ids)
            self.bar.configure(value=0)
            self.progress_var.set("0 B / unknown")
            self.speed_var.set("System network: 0 B/s")
            self.disk_speed_var.set("Disk: 0 B/s")
            self.eta_var.set("ETA --:--")
            self.status_var.set(
                f"Fresh-download cleanup complete: removed {removed} old folder(s); start is verified at 0 B"
            )
        except Exception as exc:
            messagebox.showerror("Clear old data", str(exc))

    def stop(self):
        if self.engine:
            self.engine.stop()
        self.status_var.set("Stopping… partial data will be preserved")

    def copy_log(self, _event=None):
        selected = self.log_box.selection()
        if not selected:
            return "break"
        text = "\n".join(
            str(self.log_box.item(i, "values")[0])
            for i in selected
            if self.log_box.item(i, "values")
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        return "break"

    def select_all_log(self, _event=None):
        self.log_box.selection_set(self.log_box.get_children())
        return "break"

    def log_context_menu(self, event):
        try:
            row = self.log_box.identify_row(event.y)
            if row and row not in self.log_box.selection():
                self.log_box.selection_set(row)
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()

    def add_log(self, line: str):
        self.log_box.insert("", END, values=(line,))
        items = self.log_box.get_children()
        if items:
            self.log_box.see(items[-1])
        if len(items) > 2000:
            self.log_box.delete(items[0])

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()

                # Normalize both the current (kind, payload) format and older
                # builds that queued (kind, value1, value2, ...) directly.
                if not isinstance(event, tuple) or len(event) < 2:
                    self.add_log(f"Internal event ignored: {event!r}")
                    continue
                kind = event[0]
                payload = event[1] if len(event) == 2 else event[1:]

                if kind == "log":
                    self.add_log(str(payload))
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "info":
                    self.info_var.set(str(payload))
                elif kind == "progress":
                    p = payload
                    self.bar.configure(value=min(100.0, max(0.0, p["pct"] or 0)))
                    total = human_bytes(p["total"]) if p["total"] else "unknown"
                    pct = f" ({p['pct']:.2f}%)" if p["pct"] is not None else ""
                    self.progress_var.set(f"{human_bytes(p['bytes'])} / {total}{pct}")
                    if p.get("network_available"):
                        self.speed_var.set("System network: " + human_speed(p["speed"]))
                    else:
                        self.speed_var.set("System network: unavailable")
                    self.disk_speed_var.set("Disk: " + human_speed(p.get("disk_speed", 0.0)))
                    self.eta_var.set("ETA " + eta_text(p["eta"]))
                    self.attempt_var.set(f"Attempt {p['attempt']}")
                elif kind == "export_progress":
                    b, t, name = payload
                    self.bar.configure(value=(b / t * 100.0) if t else 0)
                    self.progress_var.set(f"Export {human_bytes(b)} / {human_bytes(t)}")
                    self.status_var.set(f"Exporting {name}")
                elif kind == "finished":
                    ok, msg = payload
                    self.status_var.set(("DONE: " if ok else "FAILED: ") + str(msg))
                elif kind == "update_ready":
                    self.status_var.set("Update downloaded. Restarting…")
                    self.update_btn.configure(state="disabled")
                    self.root.after(700, lambda p=payload: self.restart_after_update(p))
                elif kind == "update_result":
                    ok, msg = payload
                    self.update_btn.configure(state="normal")
                    self.status_var.set(str(msg) if ok else "Update failed")
                    if not ok:
                        messagebox.showerror("Update", str(msg))
                elif kind == "all_done":
                    self.start_btn.configure(state="normal")
                    self.clear_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    if self.status_var.get().startswith("Preparing") or self.status_var.get().startswith("Downloading"):
                        self.status_var.set("Queue finished")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def close(self):
        if self.engine:
            self.engine.stop()
        try:
            self.save()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        error = traceback.format_exc()
        try:
            Path(__file__).with_name("startup.log").write_text(error, encoding="utf-8")
        except Exception:
            pass
        try:
            messagebox.showerror("BO3 Workshop Downloader startup error", error)
        except Exception:
            pass
        raise
