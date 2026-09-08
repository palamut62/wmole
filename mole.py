"""
wmole — Windows port of the macOS `mole` developer-disk swiss-army knife.

Modes (select in the TUI command input with `/`, or pick a subcommand on the
CLI):

    analyze   disk explorer with category breakdown + select-and-delete
    purge     dev-folder bulk delete (node_modules, target, dist, …)
    clean     auto-clean *safe defaults* with a single confirmation
    status    live system dashboard (CPU/RAM/Disk/Net/Top procs/health score)
    optimize  run system maintenance actions (flush DNS, reset WU cache, …)
    uninstall list installed programs + remove with leftover sweep
    installers find old setup files in Downloads/Desktop

Safety: deletes go to the Recycle Bin by default (send2trash).
        `K` toggles permanent mode before a confirmed delete.
        Paths matching ~/.wmole/whitelist.txt are never touched.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import hmac
import json
import os
import platform
import queue as _queue
import re
import shutil
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from concurrent import futures as _futures
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text
from rich.panel import Panel
from rich.align import Align

try:
    from send2trash import send2trash
except Exception:
    send2trash = None  # type: ignore[assignment]

try:
    import psutil
except Exception:
    psutil = None  # type: ignore[assignment]

# ---------- Key input ----------
if os.name == "nt":
    import msvcrt

    def read_key() -> str:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(ch2, "")
        if ch == "\r":
            return "ENTER"
        if ch == "\x1b":
            return "ESC"
        if ch == " ":
            return "SPACE"
        # detect Shift held for letter keys via uppercase already (msvcrt returns upper when shifted)
        return ch  # caller does .upper() comparisons
else:  # pragma: no cover
    import termios, tty

    def read_key() -> str:  # type: ignore[misc]
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(2)
                return {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT"}.get(ch2, "ESC")
            if ch in ("\r", "\n"):
                return "ENTER"
            if ch == " ":
                return "SPACE"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------- Version ----------
__version__ = "0.9.0"
GITHUB_REPO = "palamut62/wmole"
AUTO_UPDATE_INTERVAL = 6 * 3600  # seconds between background checks

# ---------- Paths / helpers ----------
USER = Path.home()
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", USER / "AppData" / "Local"))
APPDATA = Path(os.environ.get("APPDATA", USER / "AppData" / "Roaming"))
TEMP = Path(os.environ.get("TEMP", LOCALAPPDATA / "Temp"))
WMOLE_DIR = USER / ".wmole"
CONFIG_FILE = WMOLE_DIR / "config.json"
WHITELIST_FILE = WMOLE_DIR / "whitelist.txt"
PURGE_PATHS_FILE = WMOLE_DIR / "purge_paths.txt"
DENYLIST_FILE = WMOLE_DIR / "denylist.txt"
CACHE_FILE = WMOLE_DIR / "cache.json"
COMPLETION_FILE = WMOLE_DIR / "completion.ps1"
LOG_DIR = WMOLE_DIR / "logs"
OP_LOG_FILE = LOG_DIR / "operations.log"
SECURITY_LOG_FILE = LOG_DIR / "security.jsonl"
SECURITY_KEY_FILE = WMOLE_DIR / "security.key"
QUARANTINE_DIR = WMOLE_DIR / "quarantine"
QUARANTINE_INDEX = QUARANTINE_DIR / "index.json"
JOURNAL_DB = WMOLE_DIR / "journal.db"
_SECURITY_LOG_LOCK = threading.Lock()
_SECURITY_CHAIN_STATE: dict[str, str] = {}
UPDATE_CHECK_FILE = WMOLE_DIR / "update_check.json"
PENDING_UPDATE_FILE = WMOLE_DIR / "pending_update.json"


def human_size(n: int) -> str:
    for unit, div in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n/div:.1f} {unit}"
    return f"{n} B"


def free_space_gb() -> float:
    total, used, free = shutil.disk_usage(USER.anchor or "C:\\")
    return free / 1024**3


_REPARSE_ATTR = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_reparse_dirent(entry) -> bool:
    """True for junctions/symlinks, from the flags os.scandir already cached.

    `DirEntry.is_symlink()` is not enough on Windows: a *junction* carries a
    reparse tag other than IO_REPARSE_TAG_SYMLINK, so is_symlink() reports
    False and a naive walker happily descends through it. pnpm's node_modules
    is a farm of such junctions pointing back into a shared `.pnpm` store, and
    following them turns a directory walk into a cyclic one (measured: depth
    191 and still climbing after 90s on a single node_modules). Checking the
    attribute flag instead catches both link kinds.
    """
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & _REPARSE_ATTR)
    except (AttributeError, OSError):
        try:
            return entry.is_symlink()
        except OSError:
            return False


def prune_reparse_dirs(dirpath: str, dirnames: List[str]) -> None:
    """In-place `os.walk` pruning of junction/symlink subdirectories.

    os.walk(followlinks=False) only filters entries whose is_symlink() is True,
    which lets Windows junctions through. Every walker in this module routes its
    directory pruning here so no traversal can cycle."""
    if not dirnames:
        return
    kept = []
    for d in dirnames:
        try:
            attrs = os.stat(os.path.join(dirpath, d),
                            follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
            if attrs & _REPARSE_ATTR:
                continue
        except (AttributeError, OSError):
            pass
        kept.append(d)
    dirnames[:] = kept


def dir_size(path: Path, on_progress=None, max_seconds: Optional[float] = None,
             max_files: Optional[int] = None, on_truncated=None) -> int:
    """Total bytes under `path`.

    Never descends into reparse points (see is_reparse_dirent) — their content
    is counted where it physically lives, so linked trees are neither re-walked
    nor double counted. `on_truncated` is called with no arguments when a
    budget limit cut the walk short, so callers can label the result as a
    lower bound instead of presenting it as exact.
    """
    total = 0
    last_emit = 0
    started = time.time()
    seen = 0

    def walk(p) -> bool:
        """Returns False when a budget limit is hit (stop signal)."""
        nonlocal total, last_emit, seen
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if is_reparse_dirent(entry):
                                continue
                            if not walk(entry.path):
                                return False
                            continue
                        total += entry.stat(follow_symlinks=False).st_size
                        seen += 1
                    except OSError:
                        continue
                    if max_files is not None and seen >= max_files:
                        return False
                    if max_seconds is not None and time.time() - started >= max_seconds:
                        return False
                    if on_progress and total - last_emit > 25_000_000:
                        on_progress(total)
                        last_emit = total
        except OSError:
            return True
        return True

    try:
        if Path(path).is_file():
            try:
                return os.stat(path).st_size
            except OSError:
                return 0
        if not walk(path) and on_truncated:
            on_truncated()
    except OSError:
        pass
    if on_progress:
        on_progress(total)
    return total


def load_size_cache() -> dict:
    """Returns {path_str: {"mtime": float, "size": int, "scanned_at": float}}."""
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_size_cache(cache: dict) -> None:
    try:
        WMOLE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


# A directory's mtime only changes when its *direct* children change, so a
# cached size can be stale for deep trees. A TTL bounds that staleness.
CACHE_TTL_SECONDS = 24 * 3600


def cache_get(cache: dict, path: Path, mtime: float) -> Optional[int]:
    entry = cache.get(str(path))
    if not entry or abs(entry.get("mtime", -1) - mtime) >= 1e-6:
        return None
    if time.time() - entry.get("scanned_at", 0) > CACHE_TTL_SECONDS:
        return None
    return int(entry.get("size", 0))


def cache_set(cache: dict, path: Path, mtime: float, size: int) -> None:
    cache[str(path)] = {"mtime": mtime, "size": size, "scanned_at": time.time()}


# Upper bound on how long a single artifact directory may be sized. One
# pathological tree must not be able to stall a whole scan; when the budget is
# hit the size is reported as a lower bound (Item.truncated) rather than exact.
PURGE_DIR_BUDGET_SECONDS = 20.0


def sized_dir(path: Path, cache: Optional[dict] = None,
              lock: "Optional[threading.Lock]" = None,
              max_seconds: Optional[float] = None) -> tuple[int, bool]:
    """Size a dir via the mtime cache. Returns (size, truncated).

    `truncated` is True when `max_seconds` cut the walk short, meaning the size
    is a lower bound. Truncated results are deliberately not cached."""
    mtime = None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        pass
    if cache is not None and mtime is not None:
        if lock is not None:
            with lock:
                hit = cache_get(cache, path, mtime)
        else:
            hit = cache_get(cache, path, mtime)
        if hit is not None:
            return hit, False
    cut = []
    size = dir_size(path, max_seconds=max_seconds,
                    on_truncated=lambda: cut.append(True))
    truncated = bool(cut)
    if cache is not None and mtime is not None and not truncated:
        if lock is not None:
            with lock:
                cache_set(cache, path, mtime, size)
        else:
            cache_set(cache, path, mtime, size)
    return size, truncated


def should_redraw(last_draw: float, now: float, scanner_active: bool,
                  min_interval: float = 0.08) -> bool:
    """Throttle in-loop re-renders: only redraw while scanning and after min_interval."""
    if not scanner_active:
        return False
    return (now - last_draw) >= min_interval


# ---------- i18n ----------
STRINGS: Dict[str, Dict[str, str]] = {
    "tr": {
        "title_analyze":    "Disk Analizi",
        "title_inside":     "İçinde · {name}",
        "title_status":     "Sistem Durumu",
        "title_optimize":   "Sistem Optimize",
        "title_uninstall":  "Programları Kaldır",
        "free_suffix":      "({free:.1f} GB boş)",
        "mode_trash":       "🗑 Geri Dönüşüm Kutusu",
        "mode_permanent":   "⚠ KALICI MOD",
        "mode_dry_run":     "KURU ÇALIŞTIRMA",
        "hint_cats":        "Temizlenecek kategorileri seç — [Boşluk] işaretle, [D] sil, [K] kalıcı mod, [R] yenile, [/] komutlar, [?] yardım, [\\] dil",
        "hint_items":       "İçinde {name} — [Boşluk] işaretle, [D] sil",
        "hint_fs":          "[Enter] aç · [G] büyük dosyalar · [V] sürücüler · [O] Explorer · [Boşluk]/[D] sil · [/] komutlar · [?] yardım",
        "hint_status":      "Canlı sistem durumu (otomatik yenilenir) — [Q]/[Esc] geri",
        "hint_optimize":    "Optimize işlemleri — [Enter] çalıştır (yönetici gerekebilir)",
        "hint_uninstall":   "Kurulu programlar — [Enter] kaldır, [L] artıklar",
        "confirm_header":   "Aşağıdaki {n} öğe {action} ({size}).",
        "confirm_keys":     "[Enter] onayla   ·   [Esc] iptal",
        "and_more":         "  … ve {n} öğe daha",
        "on_disk_note":     "Diskte gerçekten boşalacak: {size} (mantıksal {logical})",
        "on_disk_partial":  "Ölçüm bütçesi doldu — gerçek rakam bundan yüksek olabilir.",
        "placeholder_note": "{n} bulut yer tutucusu ({size}) yalnızca bulutta duruyor: silmek yer açmaz.",
        "risk_note":        "En yüksek risk puanı: {score}/100 ({level}) — {path}",
        "protected_header": "⛔ Atlanacak (korumalı) {n} öğe:",
        "act_trash":        "Geri Dönüşüm Kutusu'na taşınacak",
        "act_permanent":    "KALICI olarak silinecek",
        "act_dry_run":      "Kuru çalıştırma · silmiş gibi yapılacak",
        "done_ok":          "✅ {n} öğe taşındı · {size} kazanıldı",
        "done_perm":        "✅ {n} öğe kalıcı silindi · {size} kazanıldı",
        "done_dry":         "ℹ Kuru çalıştırma · {n} öğe ({size}) silinecekti, hiçbir şey değişmedi",
        "done_errs":        "  ({n} hata)",
        "cancelled":        "Silme iptal edildi (Enter dışında bir tuşa basıldı).",
        "confirm_title":    "  SİLME ONAYI  ",
        "nothing_selected": "Hiçbir şey seçilmedi.",
        "permanent_on":     "⚠ KALICI MOD AÇIK — sonraki silme geri alınamaz!",
        "permanent_off":    "Güvenli mod (Geri Dönüşüm Kutusu).",
        "lang_switched":    "Dil: Türkçe",
        "footer_more":      "↑ üstte {n} öğe daha",
        "footer_less":      "↓ altta {n} öğe daha",
        "title_help":       "Yardım & Özellikler",
        "hint_help":        "[↑/↓] kaydır  ·  [Q]/[Esc] kapat",
        "help_key":         "H Yardım",
    },
    "en": {
        "title_analyze":    "Analyze Disk",
        "title_inside":     "Inside · {name}",
        "title_status":     "System Status",
        "title_optimize":   "System Optimize",
        "title_uninstall":  "Uninstall Programs",
        "free_suffix":      "({free:.1f} GB free)",
        "mode_trash":       "🗑 Recycle Bin",
        "mode_permanent":   "⚠ PERMANENT MODE",
        "mode_dry_run":     "DRY-RUN",
        "hint_cats":        "Select cleanable categories — [Space] pick, [D] delete, [K] permanent, [R] refresh, [/] commands, [?] help, [\\] lang",
        "hint_items":       "Inside {name} — [Space] pick, [D] delete",
        "hint_fs":          "[Enter] open · [G] large files · [V] drives · [O] Explorer · [Space]/[D] delete · [/] commands · [?] help",
        "hint_status":      "Live system status (auto-refresh) — [Q]/[Esc] back",
        "hint_optimize":    "Optimize actions — [Enter] run (admin may be required)",
        "hint_uninstall":   "Installed programs — [Enter] uninstall, [L] leftovers",
        "confirm_header":   "The following {n} item(s) will be {action} ({size}).",
        "confirm_keys":     "[Enter] confirm   ·   [Esc] cancel",
        "and_more":         "  … and {n} more",
        "on_disk_note":     "Actually freed on disk: {size} (logical {logical})",
        "on_disk_partial":  "Measurement budget hit — the real figure may be higher.",
        "placeholder_note": "{n} cloud placeholder(s) ({size}) live only in the cloud: deleting frees no space.",
        "risk_note":        "Highest risk score: {score}/100 ({level}) — {path}",
        "protected_header": "⛔ Protected — will be skipped ({n}):",
        "act_trash":        "moved to the Recycle Bin",
        "act_permanent":    "PERMANENTLY DELETED",
        "act_dry_run":      "dry-run · nothing will change",
        "done_ok":          "✅ {n} item(s) moved · {size} reclaimed",
        "done_perm":        "✅ {n} item(s) permanently deleted · {size} reclaimed",
        "done_dry":         "ℹ Dry-run · would delete {n} item(s) ({size}), nothing changed",
        "done_errs":        "  ({n} error(s))",
        "cancelled":        "Delete cancelled (a key other than Enter was pressed).",
        "confirm_title":    "  DELETE CONFIRMATION  ",
        "nothing_selected": "Nothing selected.",
        "permanent_on":     "⚠ PERMANENT MODE ON — next delete cannot be undone!",
        "permanent_off":    "Safe mode (Recycle Bin).",
        "lang_switched":    "Language: English",
        "footer_more":      "↑ {n} more above",
        "footer_less":      "↓ {n} more below",
        "title_help":       "Help & Features",
        "hint_help":        "[↑/↓] scroll  ·  [Q]/[Esc] close",
        "help_key":         "H Help",
    },
}
def _detect_system_lang() -> str:
    """Best-effort detection of the OS UI language.

    Returns a key present in STRINGS. Turkish systems map to 'tr'; everything
    else falls back to 'en' so non-Turkish users get an English interface by
    default. Any failure falls back to 'en'."""
    code = ""
    # 1) Windows: the actual UI language (most reliable on Windows).
    if os.name == "nt":
        try:
            import ctypes
            for fn in ("GetUserDefaultUILanguage", "GetUserDefaultLCID"):
                try:
                    lcid = getattr(ctypes.windll.kernel32, fn)()
                    import locale as _loc
                    code = _loc.windows_locale.get(lcid, "")
                    if code:
                        break
                except Exception:
                    continue
        except Exception:
            code = ""
    # 2) Cross-platform / env fallback.
    if not code:
        try:
            import locale as _loc
            code = (_loc.getlocale()[0] or "") or os.environ.get("LANG", "")
        except Exception:
            code = os.environ.get("LANG", "")
    code = (code or "").lower()
    if code.startswith("tr") or "turkish" in code:
        return "tr"
    return "en"


def _initial_lang() -> str:
    env = os.environ.get("WMOLE_LANG", "").lower()
    if env in STRINGS:
        return env
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else "{}"
        v = (json.loads(raw) or {}).get("lang", "")
        if v in STRINGS:
            return v
    except Exception:
        pass
    # No explicit preference yet — follow the system UI language (default 'en').
    return _detect_system_lang()


LANG: str = _initial_lang()


def T(key: str, **kw) -> str:
    s = STRINGS.get(LANG, STRINGS["tr"]).get(key) or STRINGS["en"].get(key, key)
    return s.format(**kw) if kw else s


def set_lang(code: str) -> None:
    global LANG
    if code in STRINGS:
        LANG = code
        try:
            cfg = load_config()
            cfg["lang"] = code
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass


def path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


# mtime-keyed caches so hot paths (is_protected_path during batch deletes)
# don't re-read config/denylist from disk for every single target.
_CONFIG_CACHE: dict = {}
_LIST_CACHE: dict = {}


def _file_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return -1.0


def _debug_log(msg: str) -> None:
    """Best-effort trace for errors that must not interrupt the user flow."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "debug.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{msg}\n")
    except OSError:
        pass


def load_config() -> dict:
    defaults = {
        "large_file_min_mb": 512,
        "analyze_start_path": str(USER),
        "protected_defaults": [
            r"C:\Windows",
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            r"C:\ProgramData",
        ],
        "quarantine_enabled": True,
        "quarantine_retention_days": 30,
        # Take a content digest when quarantining and verify it on restore.
        "quarantine_verify": True,
        "protected_processes": [
            "code.exe", "devenv.exe", "docker desktop.exe", "com.docker.backend.exe",
            "postgres.exe", "mysqld.exe", "mongod.exe",
        ],
    }
    if not path_exists(CONFIG_FILE):
        try:
            WMOLE_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(defaults, indent=2), encoding="utf-8")
            if not path_exists(DENYLIST_FILE):
                DENYLIST_FILE.write_text("# one path per line\n", encoding="utf-8")
            if not path_exists(WHITELIST_FILE):
                WHITELIST_FILE.write_text("# one path per line\n", encoding="utf-8")
            if not path_exists(PURGE_PATHS_FILE):
                PURGE_PATHS_FILE.write_text("# one path per line\n", encoding="utf-8")
        except Exception as exc:
            _debug_log(f"load_config: could not create defaults: {exc}")
        return defaults
    mtime = _file_mtime(CONFIG_FILE)
    cached = _CONFIG_CACHE.get(str(CONFIG_FILE))
    if cached and cached[0] == mtime:
        return dict(cached[1])
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            merged = defaults.copy()
            merged.update(loaded)
            _CONFIG_CACHE[str(CONFIG_FILE)] = (mtime, dict(merged))
            return merged
        _debug_log("load_config: config.json is not a JSON object, using defaults")
    except Exception as exc:
        _debug_log(f"load_config: could not read config.json: {exc}")
    return defaults


def load_path_list(path_file: Path) -> List[Path]:
    out: List[Path] = []
    if not path_exists(path_file):
        return out
    mtime = _file_mtime(path_file)
    cached = _LIST_CACHE.get(str(path_file))
    if cached and cached[0] == mtime:
        return list(cached[1])
    try:
        for line in path_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(Path(os.path.expandvars(os.path.expanduser(line))))
    except (OSError, ValueError) as exc:
        _debug_log(f"load_path_list: could not read {path_file}: {exc}")
        return out
    _LIST_CACHE[str(path_file)] = (mtime, list(out))
    return out


def load_whitelist(path_file: Path = WHITELIST_FILE) -> List[Path]:
    return load_path_list(path_file)


def load_denylist(path_file: Path = DENYLIST_FILE) -> List[Path]:
    return load_path_list(path_file)


def is_whitelisted(path: Path, wl: List[Path]) -> bool:
    p = path.resolve() if path.exists() else path
    p_str = str(p).rstrip("\\/").lower()
    for w in wl:
        try:
            w_str = str(w).rstrip("\\/").lower()
            if p_str == w_str or p_str.startswith(w_str + os.sep):
                return True
        except Exception:
            pass
    return False


def is_protected_path(path: Path) -> bool:
    """Block broad or system-owned deletion targets."""
    cfg = load_config()
    denylist = load_denylist()
    try:
        p = path.resolve() if path.exists() else path.absolute()
    except OSError:
        p = path
    raw = str(p).rstrip("\\/").lower()
    anchor = str(p.anchor).rstrip("\\/").lower()
    if raw == anchor:
        return True
    protected = [str(Path(v)).rstrip("\\/").lower() for v in cfg.get("protected_defaults", [])]
    user_root = str(USER).rstrip("\\/").lower()
    if raw == user_root:
        return True
    for d in denylist:
        dn = str(d).rstrip("\\/").lower()
        if dn and (raw == dn or raw.startswith(dn + "\\")):
            return True
    if ".git" in [part.lower() for part in p.parts]:
        return True
    if path_exists(p / ".git"):
        return True
    return any(raw == base or raw.startswith(base + "\\") for base in protected)


class ScanFilter:
    """Whitelist/denylist/protection check hoisted out of the per-candidate path.

    `is_protected_path()` re-reads config.json and denylist.txt, calls
    `Path.resolve()` and stats `<dir>/.git` on *every* call — measured at 27 ms
    per candidate, i.e. 14s of pure overhead for a 512-candidate scan. A scan
    tests thousands of paths against lists that cannot change mid-scan, so the
    lists are snapshotted once here.

    resolve() is kept only where it can matter. Candidates come from walkers
    that never cross a reparse point, so no *parent* component of a candidate
    can be a link; only the roots (resolved once, up front) and the candidate's
    own final component need the full realpath.

    This is a scan-time filter, not the safety gate: `delete_path()` still runs
    the authoritative `is_protected_path()` on every target before removing it.
    """

    def __init__(self, whitelist: Optional[List[Path]] = None) -> None:
        cfg = load_config()
        self.protected = [str(Path(v)).rstrip("\\/").lower()
                          for v in cfg.get("protected_defaults", [])]
        self.denylist = [str(d).rstrip("\\/").lower() for d in load_denylist()]
        self.denylist = [d for d in self.denylist if d]
        self.whitelist = [str(w).rstrip("\\/").lower() for w in (whitelist or [])]
        self.user_root = str(USER).rstrip("\\/").lower()

    @staticmethod
    def resolve_root(root: Path) -> Path:
        """Roots are few and may themselves be junctions — resolve them once."""
        try:
            return root.resolve()
        except OSError:
            return root

    def _key(self, path: Path) -> tuple[str, Path]:
        try:
            p = path.resolve() if _is_reparse_point(path) else Path(os.path.abspath(path))
        except OSError:
            p = path
        return str(p).rstrip("\\/").lower(), p

    def rejects(self, path: Path) -> bool:
        """True when `path` must not be offered as a cleanup candidate."""
        raw, p = self._key(path)
        for w in self.whitelist:
            if raw == w or raw.startswith(w + os.sep):
                return True
        if raw == str(p.anchor).rstrip("\\/").lower() or raw == self.user_root:
            return True
        for d in self.denylist:
            if raw == d or raw.startswith(d + "\\"):
                return True
        if any(raw == base or raw.startswith(base + "\\") for base in self.protected):
            return True
        if ".git" in [part.lower() for part in p.parts]:
            return True
        return path_exists(p / ".git")


def parse_paths_arg(text: str) -> List[Path]:
    if not text.strip():
        return []
    out: List[Path] = []
    for part in re.split(r"[;,]", text):
        part = part.strip()
        if not part:
            continue
        p = Path(os.path.expandvars(os.path.expanduser(part)))
        if p not in out:
            out.append(p)
    return out


def log_operation(action: str, path: Path, size: int = 0, result: str = "") -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with OP_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{action}\t{size}\t{path}\t{result}\n")
        log_security_event(action, {"path": str(path), "size": size, "result": result})
    except OSError:
        pass


def _security_key() -> bytes:
    WMOLE_DIR.mkdir(parents=True, exist_ok=True)
    if not path_exists(SECURITY_KEY_FILE):
        SECURITY_KEY_FILE.write_text(secrets.token_hex(32), encoding="ascii")
    return bytes.fromhex(SECURITY_KEY_FILE.read_text(encoding="ascii").strip())


def log_security_event(event: str, details: dict) -> None:
    """Append a tamper-evident HMAC-chained security event."""
    try:
        with _SECURITY_LOG_LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            state_key = str(SECURITY_LOG_FILE)
            previous = _SECURITY_CHAIN_STATE.get(state_key)
            if previous is None:
                previous = "0" * 64
                if path_exists(SECURITY_LOG_FILE):
                    lines = SECURITY_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                    if lines:
                        previous = str(json.loads(lines[-1]).get("signature", previous))
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                "details": details,
                "previous": previous,
            }
            payload = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            signature = hmac.new(_security_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            record["signature"] = signature
            with SECURITY_LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            _SECURITY_CHAIN_STATE[state_key] = signature
    except Exception as exc:
        _debug_log(f"security log failed: {exc}")


def read_security_log(limit: int = 100) -> tuple[list[dict], bool]:
    if not path_exists(SECURITY_LOG_FILE):
        return [], True
    try:
        rows = [json.loads(line) for line in SECURITY_LOG_FILE.read_text(
            encoding="utf-8", errors="strict").splitlines() if line.strip()]
        previous = "0" * 64
        valid = True
        key = _security_key()
        for row in rows:
            signature = row.pop("signature", "")
            valid = valid and row.get("previous") == previous
            payload = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            expected = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
            valid = valid and hmac.compare_digest(signature, expected)
            row["signature"] = signature
            previous = signature
        return rows[-max(1, limit):], valid
    except Exception:
        return [], False


def _git_repository(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        if path_exists(parent / ".git"):
            return parent
    return None


def git_delete_risk(path: Path) -> str:
    """Return a block reason when target contains tracked/untracked Git changes."""
    repo = _git_repository(path)
    if repo is None:
        return ""
    try:
        relative = path.resolve().relative_to(repo.resolve())
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--", str(relative)],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        if result.returncode != 0:
            return "git status could not verify target"
        if result.stdout.strip():
            return "git target contains modified or untracked files"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "git safety check failed"
    return ""


def _quarantine_entries() -> list[dict]:
    try:
        data = json.loads(QUARANTINE_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_quarantine_entries(entries: list[dict]) -> None:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_INDEX.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def quarantine_path(path: Path, size: int = 0) -> str | None:
    risk = git_delete_risk(path)
    if risk:
        log_security_event("git-delete-blocked", {"path": str(path), "reason": risk})
        return risk
    if not path_exists(path):
        return "path not found"
    entry_id = f"{int(time.time())}-{secrets.token_hex(4)}"
    target_dir = QUARANTINE_DIR / entry_id
    target = target_dir / path.name
    # Digest the content *before* it moves, so restore can prove that what
    # comes back is byte-for-byte what went in.
    digest, digest_partial = ("", False)
    if load_config().get("quarantine_verify", True):
        digest, digest_partial = content_digest(path)
    # The intent is durable before the filesystem is touched: if we die halfway
    # through the move, recovery finds this row and reconciles it.
    journal_id = JOURNAL.begin("quarantine", path, size=size, stored=str(target),
                               digest=digest, digest_algo=DIGEST_ALGO)
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
        JOURNAL.mark(journal_id, "in_progress")
        shutil.move(str(path), str(target))
        entries = _quarantine_entries()
        entries.append({
            "id": entry_id, "original": str(path), "stored": str(target),
            "size": size, "created": int(time.time()), "name": path.name,
            "digest": digest, "digest_algo": DIGEST_ALGO if digest else "",
            "digest_partial": digest_partial,
        })
        _save_quarantine_entries(entries)
        JOURNAL.mark(journal_id, "committed")
        log_operation("delete-quarantine", path, size=size, result=entry_id)
        return None
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        JOURNAL.mark(journal_id, "failed", str(exc))
        return f"quarantine: {exc}"


def restore_quarantine(entry_id: str) -> str | None:
    entries = _quarantine_entries()
    entry = next((row for row in entries if row.get("id") == entry_id), None)
    if not entry:
        return "quarantine entry not found"
    source, destination = Path(entry["stored"]), Path(entry["original"])
    if path_exists(destination):
        return "original path already exists"
    if not path_exists(source):
        return "quarantined content is missing"
    # Refuse to hand back content that changed while it sat in quarantine.
    mismatch = verify_digest(source, entry.get("digest", ""),
                             entry.get("digest_algo", ""))
    if mismatch:
        log_security_event("quarantine-integrity-failed",
                           {"id": entry_id, "path": str(destination), "reason": mismatch})
        return f"restore refused: {mismatch}"
    journal_id = JOURNAL.begin("restore", destination,
                               size=int(entry.get("size") or 0), stored=str(source),
                               digest=entry.get("digest", ""),
                               digest_algo=entry.get("digest_algo", ""))
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL.mark(journal_id, "in_progress")
        shutil.move(str(source), str(destination))
        _save_quarantine_entries([row for row in entries if row.get("id") != entry_id])
        shutil.rmtree(QUARANTINE_DIR / entry_id, ignore_errors=True)
        JOURNAL.mark(journal_id, "committed")
        log_security_event("quarantine-restored", {"id": entry_id, "path": str(destination)})
        return None
    except (OSError, shutil.Error) as exc:
        JOURNAL.mark(journal_id, "failed", str(exc))
        return f"restore: {exc}"


def purge_expired_quarantine() -> int:
    entries = _quarantine_entries()
    retention = max(1, int(load_config().get("quarantine_retention_days", 30))) * 86400
    cutoff = int(time.time()) - retention
    kept: list[dict] = []
    removed = 0
    for entry in entries:
        if int(entry.get("created", 0)) < cutoff:
            shutil.rmtree(QUARANTINE_DIR / str(entry.get("id", "")), ignore_errors=True)
            removed += 1
        else:
            kept.append(entry)
    if removed:
        _save_quarantine_entries(kept)
        log_security_event("quarantine-expired", {"removed": removed})
    return removed


def _onerr_chmod(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _is_reparse_point(path: Path) -> bool:
    """True for Windows junctions/symlinks (reparse points), incl. dir junctions
    that pathlib's is_symlink() misses. Used to avoid rmtree following the link."""
    try:
        attrs = os.stat(path, follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        try:
            return path.is_symlink()
        except OSError:
            return False


def delete_path(path: Path, use_trash: bool, dry_run: bool, known_size: int = 0) -> Optional[str]:
    """Returns None on success, error string otherwise.

    known_size: pre-computed size from the scanner (avoids redundant dir_size walk).
    """
    if is_protected_path(path):
        msg = "protected path"
        log_operation("delete-blocked", path, result=msg)
        return msg
    # Use provided known_size when available to avoid slow re-computation
    if known_size > 0:
        size = known_size
    else:
        size = 0
        try:
            size = path.stat().st_size if path.is_file() else quick_size(path)
        except OSError:
            pass
    if dry_run:
        log_operation("delete-dry-run", path, size=size, result="ok")
        return None
    if use_trash:
        if load_config().get("quarantine_enabled", True):
            return quarantine_path(path, size=size)
        if send2trash is None:
            # Safe mode requested but recycle-bin support is unavailable.
            # Do NOT silently fall back to permanent deletion.
            msg = "recycle bin unavailable (send2trash not installed)"
            log_operation("delete-blocked", path, size=size, result=msg)
            return msg
        try:
            send2trash(str(path))
            log_operation("delete-trash", path, size=size, result="ok")
            return None
        except Exception as e:
            # fall through to permanent delete? No — surface the error.
            msg = f"trash: {e}"
            log_operation("delete-trash", path, size=size, result=msg)
            return msg
    journal_id = JOURNAL.begin("delete-permanent", path, size=size)
    try:
        JOURNAL.mark(journal_id, "in_progress")
        error = permanent_delete(path)
        if error:
            JOURNAL.mark(journal_id, "failed", error)
            log_operation("delete-permanent", path, size=size, result=error)
            return error
        JOURNAL.mark(journal_id, "committed")
        log_operation("delete-permanent", path, size=size, result="ok")
        return None
    except Exception as e:
        JOURNAL.mark(journal_id, "failed", str(e))
        log_operation("delete-permanent", path, size=size, result=str(e))
        return str(e)


def _delete_guard(resolved: Path) -> str:
    """Safety checks run against the path a handle actually resolves to.

    Called after the handle is open, so what is checked and what is deleted are
    provably the same object.
    """
    if is_protected_path(resolved):
        return "protected path"
    if is_whitelisted(resolved, load_whitelist()):
        return "whitelisted path"
    return git_delete_risk(resolved)


def permanent_delete(path: Path) -> Optional[str]:
    """Remove `path` for good, preferring handle-addressed deletion.

    On Windows the object is opened once and deleted through that handle, with
    the guards evaluated against the handle's resolved path. Where no handle
    API exists we fall back to the name-based removal.
    """
    if handle_delete_supported():
        error = handle_delete(path, guard=_delete_guard)
        if error is None:
            return None
        # Genuine refusals must surface; only a missing handle API falls back.
        if "unavailable on this platform" not in error:
            return error
    if path.is_file() or path.is_symlink() or _is_reparse_point(path):
        # Remove the link/junction itself; never recurse into its target.
        try:
            path.unlink()
        except (IsADirectoryError, PermissionError, OSError):
            os.rmdir(path)
    else:
        shutil.rmtree(path, onerror=_onerr_chmod)
    return None


def trash_paths_batch(paths: List[Path],
                      sizes: Optional[Dict[str, int]] = None) -> Dict[str, Optional[str]]:
    """Send many paths to the Recycle Bin in a single IFileOperation batch.

    send2trash accepts a list and processes it in one COM operation, which is
    dramatically faster than one call per path. Returns {str(path): error|None}.
    On batch failure, falls back to per-path so one bad entry can't sink the
    whole chunk. `sizes` (keyed by str(path)) is logged so cleanup history can
    report freed bytes."""
    result: Dict[str, Optional[str]] = {}
    if not paths:
        return result
    sizes = sizes or {}
    if load_config().get("quarantine_enabled", True):
        for p in paths:
            result[str(p)] = quarantine_path(p, size=sizes.get(str(p), 0))
        return result
    if send2trash is None:
        msg = "recycle bin unavailable (send2trash not installed)"
        for p in paths:
            log_operation("delete-blocked", p, result=msg)
            result[str(p)] = msg
        return result
    try:
        send2trash([str(p) for p in paths])
        for p in paths:
            log_operation("delete-trash", p, size=sizes.get(str(p), 0), result="ok")
            result[str(p)] = None
        return result
    except Exception:
        # Batch failed — isolate the culprit by retrying individually.
        for p in paths:
            try:
                send2trash(str(p))
                log_operation("delete-trash", p, size=sizes.get(str(p), 0), result="ok")
                result[str(p)] = None
            except Exception as e:
                msg = f"trash: {e}"
                log_operation("delete-trash", p, size=sizes.get(str(p), 0), result=msg)
                result[str(p)] = msg
        return result


# ---------- Real on-disk size / cloud placeholders ----------
# st_size is the *logical* length of a file. What a delete actually frees is
# the number of bytes the filesystem has allocated, which differs from the
# logical length in four common ways on Windows:
#
#   1. cluster rounding   - a 1-byte file occupies a whole allocation unit
#   2. sparse files       - unwritten ranges consume nothing
#   3. NTFS compression   - allocated < logical
#   4. cloud placeholders - OneDrive/Dropbox "online only" files report their
#                           full cloud length but occupy ~0 bytes locally
#
# Reporting st_size as "space you will reclaim" therefore overstates the
# result, sometimes by orders of magnitude for a synced folder. The helpers
# below compute the allocated figure so the freed-space estimate matches what
# the drive actually shows afterwards.

FILE_ATTRIBUTE_SPARSE_FILE = 0x00000200
FILE_ATTRIBUTE_COMPRESSED = 0x00000800
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

# Any one of these means the bytes live in the cloud, not on this disk.
PLACEHOLDER_ATTRS = (FILE_ATTRIBUTE_OFFLINE
                     | FILE_ATTRIBUTE_RECALL_ON_OPEN
                     | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)

_INVALID_FILE_SIZE = 0xFFFFFFFF
_kernel32 = None
_cluster_cache: Dict[str, int] = {}
_cluster_lock = threading.Lock()


def _win_kernel32():
    """Cached kernel32 handle with argtypes set, or None off Windows."""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32 or None
    if os.name != "nt":
        _kernel32 = False  # type: ignore[assignment]
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.GetCompressedFileSizeW.restype = wintypes.DWORD
        k.GetDiskFreeSpaceW.argtypes = [wintypes.LPCWSTR] + [ctypes.POINTER(wintypes.DWORD)] * 4
        k.GetDiskFreeSpaceW.restype = wintypes.BOOL
        _kernel32 = k
        return k
    except Exception:
        _kernel32 = False  # type: ignore[assignment]
        return None


def cluster_size(path: Path) -> int:
    """Allocation-unit size of the volume holding `path` (0 when unknown)."""
    try:
        root = os.path.splitdrive(os.path.abspath(str(path)))[0]
    except (OSError, ValueError):
        return 0
    if not root:
        return 0
    root = root + os.sep
    with _cluster_lock:
        hit = _cluster_cache.get(root)
    if hit is not None:
        return hit
    size = 0
    k = _win_kernel32()
    if k is not None:
        try:
            import ctypes
            from ctypes import wintypes
            spc = wintypes.DWORD()
            bps = wintypes.DWORD()
            free = wintypes.DWORD()
            total = wintypes.DWORD()
            if k.GetDiskFreeSpaceW(root, ctypes.byref(spc), ctypes.byref(bps),
                                   ctypes.byref(free), ctypes.byref(total)):
                size = int(spc.value) * int(bps.value)
        except Exception:
            size = 0
    with _cluster_lock:
        _cluster_cache[root] = size
    return size


def _round_to_cluster(nbytes: int, cluster: int) -> int:
    if cluster <= 0 or nbytes <= 0:
        return nbytes
    return ((nbytes + cluster - 1) // cluster) * cluster


def is_cloud_placeholder(attrs: int) -> bool:
    """True when the file's bytes are dehydrated to a cloud provider."""
    return bool(attrs & PLACEHOLDER_ATTRS)


def _entry_attrs(st) -> int:
    return int(getattr(st, "st_file_attributes", 0) or 0)


def file_on_disk_size(path, logical: int, attrs: int = 0, st=None) -> int:
    """Bytes this single file actually occupies on the volume.

    Cloud placeholders occupy nothing. Sparse/compressed files report their
    allocated size via GetCompressedFileSizeW. Everything else is the logical
    length rounded up to a whole allocation unit.
    """
    if is_cloud_placeholder(attrs):
        return 0
    allocated = None
    if attrs & (FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED):
        k = _win_kernel32()
        if k is not None:
            try:
                import ctypes
                from ctypes import wintypes
                high = wintypes.DWORD(0)
                low = k.GetCompressedFileSizeW(str(path), ctypes.byref(high))
                if low != _INVALID_FILE_SIZE or ctypes.get_last_error() == 0:
                    allocated = (int(high.value) << 32) | int(low)
            except Exception:
                allocated = None
    if allocated is None and os.name != "nt":
        # POSIX reports allocation directly; no cluster rounding needed.
        blocks = getattr(st, "st_blocks", None)
        if blocks is not None:
            return int(blocks) * 512
    if allocated is None:
        allocated = logical
    return _round_to_cluster(allocated, cluster_size(path))


@dataclass
class DiskFacts:
    """What a path costs and what deleting it would actually free."""
    logical: int = 0            # sum of st_size — what wmole used to report
    on_disk: int = 0            # allocated bytes: what a delete really frees
    placeholder_bytes: int = 0  # logical bytes that live only in the cloud
    placeholder_files: int = 0
    files: int = 0
    truncated: bool = False     # a budget cut the walk short; figures are lower bounds

    def as_dict(self) -> dict:
        return {
            "logical": self.logical,
            "on_disk": self.on_disk,
            "placeholder_bytes": self.placeholder_bytes,
            "placeholder_files": self.placeholder_files,
            "files": self.files,
            "truncated": self.truncated,
        }


def disk_facts(path: Path, max_seconds: Optional[float] = None,
               max_files: Optional[int] = None) -> DiskFacts:
    """Walk `path` and report logical vs. actually-allocated bytes.

    Never descends into reparse points, matching dir_size(): linked trees are
    counted where they physically live.
    """
    facts = DiskFacts()
    started = time.time()

    def account(entry_path, st) -> None:
        attrs = _entry_attrs(st)
        logical = int(st.st_size)
        facts.logical += logical
        facts.files += 1
        if is_cloud_placeholder(attrs):
            facts.placeholder_files += 1
            facts.placeholder_bytes += logical
            return
        facts.on_disk += file_on_disk_size(entry_path, logical, attrs, st)

    def walk(p) -> bool:
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if is_reparse_dirent(entry):
                                continue
                            if not walk(entry.path):
                                return False
                            continue
                        account(entry.path, entry.stat(follow_symlinks=False))
                    except OSError:
                        continue
                    if max_files is not None and facts.files >= max_files:
                        return False
                    if max_seconds is not None and time.time() - started >= max_seconds:
                        return False
        except OSError:
            return True
        return True

    try:
        st = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            account(path, st)
            return facts
    except OSError:
        return facts
    if not walk(path):
        facts.truncated = True
    return facts


def freeable_bytes(paths: List[Path], max_seconds_each: Optional[float] = 8.0) -> DiskFacts:
    """Aggregate on-disk facts for the paths a delete is about to touch.

    This is deliberately computed at confirmation time rather than during the
    scan: it costs an extra syscall per file, which is affordable for the
    handful of selected items but not for a whole-drive walk.
    """
    total = DiskFacts()
    for p in paths:
        f = disk_facts(p, max_seconds=max_seconds_each)
        total.logical += f.logical
        total.on_disk += f.on_disk
        total.placeholder_bytes += f.placeholder_bytes
        total.placeholder_files += f.placeholder_files
        total.files += f.files
        total.truncated = total.truncated or f.truncated
    return total


# ---------- Per-item explanation cards ----------
# Every candidate answers the same six questions. `rebuild` is a command shown
# to the user for reference — wmole never runs it.

@dataclass
class Explanation:
    what: str            # what this is
    lost: str            # what disappears
    kept: str            # what survives
    rebuild: str         # command that recreates it ("" when nothing can)
    regenerable: bool    # whether `rebuild` genuinely restores it
    when_freed: str      # when the space actually comes back
    reversible: str      # can this be undone, and how

    def as_dict(self) -> dict:
        return {
            "what": self.what, "lost": self.lost, "kept": self.kept,
            "rebuild": self.rebuild, "regenerable": self.regenerable,
            "when_freed": self.when_freed, "reversible": self.reversible,
        }


_FREED_IMMEDIATE = "immediate"
_FREED_ON_QUARANTINE_EXPIRY = "quarantine-expiry"
_FREED_ON_EMPTY_BIN = "empty-recycle-bin"
_FREED_ON_REBOOT = "reboot"

# kind -> (what, lost, kept, rebuild, regenerable)
EXPLAIN_TABLE: Dict[str, tuple] = {
    "npm-cache": ("npm's global package tarball cache",
                  "Cached tarballs; the next install re-downloads them",
                  "Installed node_modules, package-lock.json, your registry auth",
                  "npm cache verify", True),
    "pnpm-store": ("pnpm's content-addressable package store",
                   "The shared store; existing projects lose their hard-link targets",
                   "package.json and pnpm-lock.yaml",
                   "pnpm install --frozen-lockfile", True),
    "yarn-cache": ("Yarn's global download cache",
                   "Cached packages; the next install re-downloads them",
                   "node_modules and yarn.lock", "yarn cache clean", True),
    "pip-cache": ("pip's HTTP and wheel cache",
                  "Cached wheels; the next install rebuilds or re-downloads them",
                  "Installed packages and virtualenvs", "pip cache purge", True),
    "cargo-cache": ("Cargo's downloaded crate archives",
                    "Crate .crate files; the next build re-fetches them",
                    "Cargo.lock, target/, installed binaries", "cargo fetch", True),
    "go-cache": ("Go build cache",
                 "Compiled package objects; the next build is slower once",
                 "Module cache and source", "go build ./...", True),
    "gradle-cache": ("Gradle's global dependency and build cache",
                     "Cached dependencies and build outputs; next build re-downloads",
                     "Project sources and gradle wrapper", "gradle build", True),
    "maven-repo": ("Local Maven repository",
                   "Every cached artifact, including any you installed locally "
                   "with mvn install and cannot re-download",
                   "Project sources and pom.xml", "mvn dependency:go-offline", False),
    "nuget-cache": ("Global NuGet package folder",
                    "Extracted packages; restore re-downloads them",
                    "Project files and packages.lock.json", "dotnet restore", True),
    "conda-pkgs": ("Conda package cache",
                   "Cached packages; new environments re-download them",
                   "Existing environments (they hard-link, so links are broken)",
                   "conda clean --packages", True),
    "node_modules": ("Installed dependencies for one project",
                     "The installed tree, including any locally patched files",
                     "package.json, the lockfile, and your source",
                     "npm ci", True),
    "target": ("Rust/Java build output directory",
               "Compiled artifacts; the next build is a full rebuild",
               "Cargo.toml/pom.xml and source", "cargo build", True),
    "venv": ("Python virtual environment",
             "The environment and anything pip-installed into it, including "
             "editable installs not captured in requirements",
             "Your source and requirements.txt",
             "python -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt", False),
    "__pycache__": ("Compiled Python bytecode",
                    "Cached .pyc files; the next import recompiles them",
                    "All .py sources", "", True),
    "dist": ("Packaging output directory",
             "Built artifacts; anything already published elsewhere is unaffected",
             "Source and build config", "npm run build", True),
    "build": ("Build output directory",
              "Compiled output; the next build recreates it",
              "Source and build config", "npm run build", True),
    ".next": ("Next.js build and incremental cache",
              "Prerendered pages and the incremental cache; next build is slower",
              "Source, next.config, and .env", "npx next build", True),
    "browser-cache": ("Browser HTTP/media cache",
                      "Cached pages and media; sites reload slower once",
                      "Cookies, logins, history, bookmarks, extensions", "", True),
    "cache": ("A cache directory wmole does not recognise specifically",
              "Cached data; whatever wrote it will rebuild it on demand — "
              "but wmole cannot promise that for an unknown tool",
              "The application and its configuration", "", False),
    "temp": ("Temporary files written by the OS and applications",
             "Scratch files; anything still in use by a running program is skipped",
             "Everything outside the temp directory", "", True),
    "crash-dumps": ("Application crash dumps",
                    "Crash evidence — delete only after you are done debugging",
                    "The applications themselves", "", False),
    "win-update": ("Windows Update download cache",
                   "Downloaded update payloads; Windows re-downloads if needed",
                   "Installed updates and rollback points", "", True),
    "vscode-storage": ("VS Code per-workspace state",
                       "Workspace-local UI state and some extension caches",
                       "Settings, keybindings, extensions, and your files", "", True),
    "jetbrains": ("JetBrains IDE caches and indexes",
                  "Indexes; the IDE re-indexes on next open",
                  "Settings, plugins, and project config", "", True),
    ".idea": ("JetBrains project settings folder",
              "Run configurations, per-project settings, and local history",
              "Your source and VCS history", "", False),
    "docker-desktop": ("Docker Desktop logs and caches",
                       "Diagnostic logs and caches",
                       "Images, volumes, and containers", "", True),
    "installer": ("Downloaded installer file",
                  "The setup file; the installed program is untouched",
                  "The installed application", "re-download from the vendor", False),
    "downloads": ("An old file in Downloads",
                  "The file itself — it may be the only copy",
                  "Nothing else", "", False),
    "duplicate": ("A byte-identical copy of another file",
                  "This copy; at least one identical copy remains",
                  "The other copies of the same content", "", False),
    "large-file": ("A large user file",
                   "The file itself — it may be the only copy",
                   "Nothing else", "", False),
    "recycle-bin": ("Items already in the Recycle Bin",
                    "Restore-ability for those items",
                    "Everything outside the bin", "", False),
}

# Substrings matched against the folder name when the category has no entry.
_EXPLAIN_BY_NAME = (
    ("node_modules", "node_modules"), ("__pycache__", "__pycache__"),
    (".next", ".next"), (".venv", "venv"), ("venv", "venv"),
    ("target", "target"), ("dist", "dist"), ("build", "build"),
    (".idea", ".idea"),
)


_FIXED_KEY_INDEX: Optional[List[tuple]] = None
_FIXED_KEY_LOCK = threading.Lock()


def kind_for_path(path: Path) -> str:
    """Recover the category key for a bare path.

    The scanner already knows which category an item came from, but the CLI and
    the GUI detail panel are often handed a path alone. Matching it against the
    known cleanup locations gives a far better card than falling back to
    "unrecognised".
    """
    global _FIXED_KEY_INDEX
    with _FIXED_KEY_LOCK:
        if _FIXED_KEY_INDEX is None:
            # Longest path first so a nested location wins over its parent.
            _FIXED_KEY_INDEX = sorted(
                ((str(row[3]).rstrip("\\/").lower(), row[0])
                 for row in build_fixed_path_categories()),
                key=lambda pair: len(pair[0]), reverse=True)
        index = _FIXED_KEY_INDEX
    try:
        target = str(path).rstrip("\\/").lower()
    except Exception:
        return ""
    for prefix, key in index:
        if target == prefix or target.startswith(prefix + os.sep):
            return key
    return ""


def _explain_key(path: Path, kind: str = "") -> str:
    kind = (kind or "").lower()
    if not kind:
        kind = kind_for_path(path).lower()
    if kind in EXPLAIN_TABLE:
        return kind
    if "cache" in kind and "browser" not in kind:
        for browser in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi"):
            if browser in kind:
                return "browser-cache"
    if kind.endswith("-temp") or kind in ("user-temp", "win-temp"):
        return "temp"
    if kind in ("old-installers", "installers"):
        return "installer"
    if kind in ("old-downloads",):
        return "downloads"
    name = path.name.lower()
    for needle, key in _EXPLAIN_BY_NAME:
        if name == needle or name.startswith(needle):
            return key
    if "cache" in kind or "cache" in name:
        if any(b in name for b in ("chrome", "edge", "firefox", "brave", "opera")):
            return "browser-cache"
        return "cache"
    return ""


def explain_item(path: Path, kind: str = "") -> Explanation:
    """Answer the six questions for one cleanup candidate.

    Falls back to a deliberately conservative card for anything unrecognised:
    unknown means "assume it is your data and cannot be regenerated".
    """
    key = _explain_key(path, kind)
    row = EXPLAIN_TABLE.get(key)
    if row is None:
        what = "A file or folder wmole has no specific knowledge of"
        lost, kept, rebuild, regenerable = (
            "The path and everything under it",
            "Nothing else is affected",
            "", False,
        )
    else:
        what, lost, kept, rebuild, regenerable = row

    cfg = load_config()
    if cfg.get("quarantine_enabled", True):
        retention = max(1, int(cfg.get("quarantine_retention_days", 30)))
        when_freed = _FREED_ON_QUARANTINE_EXPIRY
        reversible = (f"Yes — moved to quarantine and restorable for {retention} days, "
                      "with a content digest checked on restore")
    elif send2trash is not None:
        when_freed = _FREED_ON_EMPTY_BIN
        reversible = "Yes — restorable from the Recycle Bin until you empty it"
    else:
        when_freed = _FREED_IMMEDIATE
        reversible = "No — permanent deletion is enabled and the Recycle Bin is unavailable"
    return Explanation(what=what, lost=lost, kept=kept, rebuild=rebuild,
                       regenerable=regenerable, when_freed=when_freed,
                       reversible=reversible)


# ---------- Per-item risk score ----------
# 0-100, built from named modifiers so the number is always explainable: the
# UI shows every modifier that contributed and how much it added.

RISK_BASE: Dict[str, int] = {
    "npm-cache": 5, "pnpm-store": 10, "yarn-cache": 5, "pip-cache": 5,
    "cargo-cache": 5, "go-cache": 5, "gradle-cache": 8, "nuget-cache": 8,
    "conda-pkgs": 12, "maven-repo": 30, "browser-cache": 8, "temp": 5,
    "__pycache__": 5, "dist": 12, "build": 12, ".next": 10,
    "node_modules": 15, "target": 15, "vscode-storage": 12, "jetbrains": 10,
    ".idea": 25, "docker-desktop": 30, "win-update": 15, "crash-dumps": 20,
    "venv": 30, "installer": 30, "downloads": 45, "duplicate": 35,
    "large-file": 50, "recycle-bin": 20,
}
RISK_DEFAULT_BASE = 45

RISK_LEVELS = ((20, "low"), (45, "moderate"), (70, "high"), (101, "critical"))


@dataclass
class RiskModifier:
    code: str
    points: int
    label: str

    def as_dict(self) -> dict:
        return {"code": self.code, "points": self.points, "label": self.label}


@dataclass
class RiskScore:
    score: int
    level: str
    modifiers: List[RiskModifier] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"score": self.score, "level": self.level,
                "modifiers": [m.as_dict() for m in self.modifiers]}


def _risk_level(score: int) -> str:
    for ceiling, name in RISK_LEVELS:
        if score < ceiling:
            return name
    return "critical"


def risk_score(path: Path, kind: str = "", check_git: bool = True) -> RiskScore:
    """Score how dangerous deleting `path` is, with every contribution named.

    `check_git` runs `git status` on the target and is the one expensive part;
    callers scoring a long list can turn it off and score the selection later.
    """
    key = _explain_key(path, kind)
    mods: List[RiskModifier] = []
    base = RISK_BASE.get(key, RISK_DEFAULT_BASE)
    mods.append(RiskModifier("base", base,
                             f"baseline for {key or 'unrecognised content'}"))

    explanation = explain_item(path, kind)
    if not explanation.regenerable:
        mods.append(RiskModifier("not-regenerable", 20,
                                 "no command reliably recreates this"))

    if is_protected_path(path):
        mods.append(RiskModifier("protected", 40,
                                 "path is on wmole's protected list"))

    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        st = None

    if st is not None:
        age = time.time() - st.st_mtime
        if age < 86400:
            mods.append(RiskModifier("modified-today", 15,
                                     "changed within the last 24 hours"))
        elif age < 7 * 86400:
            mods.append(RiskModifier("modified-recently", 8,
                                     "changed within the last 7 days"))
        if is_cloud_placeholder(_entry_attrs(st)):
            mods.append(RiskModifier("cloud-placeholder", 12,
                                     "cloud placeholder: deleting removes the "
                                     "cloud copy but frees no local space"))

    if _is_reparse_point(path):
        mods.append(RiskModifier("reparse-point", 10,
                                 "junction or symlink: the target is shared"))

    try:
        path.resolve().relative_to(USER.resolve())
    except (ValueError, OSError):
        mods.append(RiskModifier("outside-home", 10,
                                 "lives outside your user profile"))

    if check_git:
        git_reason = git_delete_risk(path)
        if git_reason:
            mods.append(RiskModifier("git-dirty", 25, git_reason))

    if load_config().get("quarantine_enabled", True):
        mods.append(RiskModifier("quarantined", -15,
                                 "recoverable from quarantine after deletion"))

    score = max(0, min(100, sum(m.points for m in mods)))
    return RiskScore(score=score, level=_risk_level(score), modifiers=mods)


# ---------- Content integrity (BLAKE3 when available) ----------
# Quarantine is only a safety net if what comes back is what went in. A digest
# is taken at move time and re-checked on restore; a mismatch refuses the
# restore instead of silently handing back altered content.

try:  # blake3 is optional; blake2b is a fine stand-in from the stdlib
    from blake3 import blake3 as _blake3  # type: ignore[import-not-found]
except Exception:
    _blake3 = None  # type: ignore[assignment]

DIGEST_ALGO = "blake3" if _blake3 is not None else "blake2b"
# Hashing is linear in bytes; past this the cost outweighs the benefit and the
# digest is recorded as partial (structure + head/tail of each file).
DIGEST_BUDGET_BYTES = 2 * 1024 ** 3
_DIGEST_CHUNK = 1024 * 1024


def _new_digest():
    return _blake3() if _blake3 is not None else hashlib.blake2b(digest_size=32)


def _hash_file_into(hasher, path: Path, full: bool) -> None:
    """Feed a file's bytes into `hasher`. When `full` is False, only the first
    and last chunk are read — enough to catch truncation and edits in place."""
    with open(path, "rb") as fh:
        if full:
            for chunk in iter(lambda: fh.read(_DIGEST_CHUNK), b""):
                hasher.update(chunk)
            return
        hasher.update(fh.read(_DIGEST_CHUNK))
        try:
            size = os.fstat(fh.fileno()).st_size
            if size > _DIGEST_CHUNK:
                fh.seek(max(0, size - _DIGEST_CHUNK))
                hasher.update(fh.read(_DIGEST_CHUNK))
        except OSError:
            pass


def content_digest(path: Path, budget: int = DIGEST_BUDGET_BYTES) -> tuple[str, bool]:
    """Return (hex_digest, partial) for a file or a whole directory tree.

    For a directory the digest covers the sorted relative path and size of
    every entry as well as file content, so a missing, renamed or resized file
    is detected even when the remaining bytes are unchanged.
    """
    hasher = _new_digest()
    partial = False
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return "", True
    if not stat.S_ISDIR(st.st_mode):
        full = st.st_size <= budget
        partial = not full
        try:
            _hash_file_into(hasher, path, full)
        except OSError:
            return "", True
        return hasher.hexdigest(), partial

    total = 0
    rows: List[tuple] = []
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        prune_reparse_dirs(root, dirs)
        dirs.sort()
        for name in sorted(files):
            fp = Path(root) / name
            try:
                fst = os.stat(fp, follow_symlinks=False)
            except OSError:
                continue
            rel = os.path.relpath(str(fp), str(path)).replace("\\", "/")
            rows.append((rel, int(fst.st_size), fp))
            total += int(fst.st_size)
    full = total <= budget
    partial = not full
    for rel, size, fp in rows:
        hasher.update(f"{rel}\0{size}\0".encode("utf-8", "surrogateescape"))
        try:
            _hash_file_into(hasher, fp, full)
        except OSError:
            partial = True
    return hasher.hexdigest(), partial


def verify_digest(path: Path, expected: str, algo: str,
                  budget: int = DIGEST_BUDGET_BYTES) -> Optional[str]:
    """None when the content matches, otherwise a human-readable reason."""
    if not expected:
        return None  # nothing was recorded; nothing to check
    if algo != DIGEST_ALGO:
        return (f"digest algorithm changed ({algo} -> {DIGEST_ALGO}); "
                "cannot verify this entry")
    actual, _partial = content_digest(path, budget=budget)
    if not actual:
        return "quarantined content could not be read for verification"
    if not hmac.compare_digest(actual, expected):
        return "quarantined content no longer matches the digest taken at delete time"
    return None


# ---------- Crash-safe operation journal ----------
# A JSON list is rewritten wholesale and is lost if the process dies mid-write,
# which is exactly when a record of what was in flight matters most. SQLite in
# WAL mode records the *intent* durably before the filesystem is touched, so a
# killed process leaves a row that recovery can reconcile.

_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id          TEXT PRIMARY KEY,
    op          TEXT NOT NULL,
    path        TEXT NOT NULL,
    stored      TEXT,
    size        INTEGER DEFAULT 0,
    digest      TEXT,
    digest_algo TEXT,
    state       TEXT NOT NULL,
    detail      TEXT,
    started     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS operations_state ON operations(state);
"""

JOURNAL_OPEN = ("intent", "in_progress")


class OperationJournal:
    """Write-ahead record of destructive operations.

    Usage is strictly: begin() -> touch the filesystem -> commit()/fail().
    Anything left in an open state after a crash is reconciled by recover().
    """

    def __init__(self, db_path: Path = JOURNAL_DB) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # The whole point is surviving a hard kill, so pay for durable commits.
        conn.execute("PRAGMA synchronous=FULL")
        if not self._ready:
            conn.executescript(_JOURNAL_SCHEMA)
            conn.commit()
            self._ready = True
        return conn

    def begin(self, op: str, path: Path, size: int = 0, stored: str = "",
              digest: str = "", digest_algo: str = "") -> Optional[str]:
        """Durably record the intent. Returns an id, or None if the journal is
        unavailable — callers treat that as "proceed without a safety net"
        rather than blocking the whole operation."""
        entry_id = f"{int(time.time())}-{secrets.token_hex(6)}"
        now = time.time()
        try:
            with self._lock, contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO operations (id, op, path, stored, size, digest,"
                    " digest_algo, state, detail, started, updated)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (entry_id, op, str(path), stored, int(size), digest,
                     digest_algo, "intent", "", now, now),
                )
            return entry_id
        except sqlite3.Error:
            return None

    def mark(self, entry_id: Optional[str], state: str, detail: str = "",
             stored: str = "", digest: str = "", digest_algo: str = "") -> None:
        if not entry_id:
            return
        sets = ["state = ?", "detail = ?", "updated = ?"]
        args: list = [state, detail, time.time()]
        if stored:
            sets.append("stored = ?")
            args.append(stored)
        if digest:
            sets.extend(["digest = ?", "digest_algo = ?"])
            args.extend([digest, digest_algo])
        args.append(entry_id)
        try:
            with self._lock, contextlib.closing(self._connect()) as conn, conn:
                conn.execute(f"UPDATE operations SET {', '.join(sets)} WHERE id = ?", args)
        except sqlite3.Error:
            pass

    def open_entries(self) -> List[dict]:
        placeholders = ",".join("?" * len(JOURNAL_OPEN))
        try:
            with self._lock, contextlib.closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    f"SELECT * FROM operations WHERE state IN ({placeholders})"
                    " ORDER BY started", JOURNAL_OPEN).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def prune(self, keep_days: int = 30) -> int:
        cutoff = time.time() - keep_days * 86400
        try:
            with self._lock, contextlib.closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    "DELETE FROM operations WHERE updated < ? AND state NOT IN"
                    f" ({','.join('?' * len(JOURNAL_OPEN))})",
                    (cutoff, *JOURNAL_OPEN))
                return cur.rowcount or 0
        except sqlite3.Error:
            return 0

    def recover(self) -> List[dict]:
        """Reconcile every operation that was in flight when we last died.

        For each open row we ask the filesystem what actually happened:
          - original still there, nothing in quarantine -> the move never
            started; the intent is void.
          - original gone, content in quarantine -> the move completed but the
            index write did not; re-attach it to the quarantine index.
          - neither -> the content is unaccounted for; flagged, never guessed.
        """
        outcomes: List[dict] = []
        entries = self.open_entries()
        if not entries:
            return outcomes
        index = _quarantine_entries()
        known = {row.get("id") for row in index}
        index_changed = False
        for row in entries:
            original = Path(row["path"])
            stored = Path(row["stored"]) if row.get("stored") else None
            has_original = path_exists(original)
            has_stored = bool(stored and path_exists(stored))
            if has_original and not has_stored:
                verdict, detail = "void", "original intact; operation never started"
            elif has_stored and not has_original:
                verdict, detail = "reattached", "content found in quarantine; index repaired"
                qid = (stored.parent.name if stored else "")
                if qid and qid not in known:
                    index.append({
                        "id": qid, "original": str(original), "stored": str(stored),
                        "size": int(row.get("size") or 0),
                        "created": int(row.get("started") or time.time()),
                        "name": original.name,
                        "digest": row.get("digest") or "",
                        "digest_algo": row.get("digest_algo") or "",
                        "recovered": True,
                    })
                    known.add(qid)
                    index_changed = True
            elif has_stored and has_original:
                verdict, detail = "duplicated", ("content exists in both places; "
                                                 "resolve manually before deleting either")
            else:
                verdict, detail = "unaccounted", "neither the original nor a quarantine copy was found"
            self.mark(row["id"], f"recovered:{verdict}", detail)
            outcomes.append({"id": row["id"], "op": row["op"], "path": row["path"],
                             "verdict": verdict, "detail": detail})
        if index_changed:
            _save_quarantine_entries(index)
        if outcomes:
            log_security_event("journal-recovered", {"count": len(outcomes)})
        return outcomes


JOURNAL = OperationJournal()


# ---------- Handle-based deletion ----------
# Checking a path and then deleting it *by name* leaves a window in which the
# name can be repointed at something else. Here the path is opened once, the
# handle's own resolved path is what the guards run against, and the delete is
# issued through that same handle — the name is never used to address the
# object a second time.

_GENERIC_DELETE = 0x00010000  # DELETE
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_DISPOSITION_INFO = 4
_INVALID_HANDLE_VALUE = -1
_VOLUME_PREFIX = "\\\\?\\"


def _handle_api():
    """kernel32 with the handle-delete entry points bound, or None."""
    k = _win_kernel32()
    if k is None:
        return None
    if getattr(k, "_wmole_handle_ready", False):
        return k
    try:
        import ctypes
        from ctypes import wintypes
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                wintypes.DWORD, wintypes.DWORD]
        k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        k.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
        k.SetFileInformationByHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k._wmole_handle_ready = True
        return k
    except Exception:
        return None


def _strip_volume_prefix(text: str) -> str:
    if text.startswith(_VOLUME_PREFIX + "UNC\\"):
        return "\\\\" + text[len(_VOLUME_PREFIX) + 4:]
    if text.startswith(_VOLUME_PREFIX):
        return text[len(_VOLUME_PREFIX):]
    return text


class DeleteHandle:
    """An open handle to a filesystem object plus the path it really resolves to.

    While this is open the object cannot be renamed or replaced (the handle is
    opened without FILE_SHARE_DELETE), which is what closes the check-then-act
    window.
    """

    def __init__(self, handle, resolved: Path, is_dir: bool) -> None:
        self.handle = handle
        self.resolved = resolved
        self.is_dir = is_dir
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        k = _handle_api()
        if k is not None and self.handle is not None:
            k.CloseHandle(self.handle)

    def delete(self) -> Optional[str]:
        """Delete through the handle itself. None on success, else an error."""
        k = _handle_api()
        if k is None or self.handle is None:
            return "handle deletion unavailable on this platform"
        try:
            import ctypes
            from ctypes import wintypes

            class _FileDispositionInfo(ctypes.Structure):
                _fields_ = [("DeleteFile", wintypes.BOOL)]

            info = _FileDispositionInfo(DeleteFile=True)
            ok = k.SetFileInformationByHandle(self.handle, _FILE_DISPOSITION_INFO,
                                              ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                return f"handle delete failed (win32 error {ctypes.get_last_error()})"
            return None
        except Exception as exc:
            return f"handle delete failed: {exc}"

    def __enter__(self) -> "DeleteHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_delete_handle(path: Path) -> tuple[Optional[DeleteHandle], Optional[str]]:
    """Open `path` for deletion and resolve its true path from the handle.

    Reparse points are opened as themselves, never followed: deleting a
    junction must remove the link, not the tree behind it.
    """
    k = _handle_api()
    if k is None:
        return None, "handle deletion unavailable on this platform"
    try:
        import ctypes
        from ctypes import wintypes
        flags = _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT
        handle = k.CreateFileW(str(path), _GENERIC_DELETE | _FILE_READ_ATTRIBUTES,
                               _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                               _OPEN_EXISTING, flags, None)
        if handle in (None, _INVALID_HANDLE_VALUE) or ctypes.c_void_p(handle).value in (
                None, ctypes.c_void_p(_INVALID_HANDLE_VALUE).value):
            return None, f"cannot open target (win32 error {ctypes.get_last_error()})"
        buf = ctypes.create_unicode_buffer(32768)
        length = k.GetFinalPathNameByHandleW(handle, buf, 32767, 0)
        if length == 0 or length > 32767:
            k.CloseHandle(handle)
            return None, f"cannot resolve target (win32 error {ctypes.get_last_error()})"
        resolved = Path(_strip_volume_prefix(buf.value))
        try:
            is_dir = stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode)
        except OSError:
            is_dir = False
        return DeleteHandle(handle, resolved, is_dir), None
    except Exception as exc:
        return None, f"cannot open target: {exc}"


def handle_delete(path: Path, guard=None) -> Optional[str]:
    """Delete `path` addressing it only through an open handle.

    `guard(resolved_path)` is called with the path the handle actually resolves
    to and may return a reason to abort. This is the whole point: the guard
    sees the real object, not the name the caller typed.

    Returns None on success, an error string otherwise. Falls back to the
    name-based path only where the platform provides no handle API.
    """
    root, err = open_delete_handle(path)
    if root is None:
        return err
    with root:
        resolved = root.resolved
        if guard is not None:
            reason = guard(resolved)
            if reason:
                log_security_event("handle-delete-blocked",
                                   {"path": str(path), "resolved": str(resolved),
                                    "reason": reason})
                return reason
        if not root.is_dir or _is_reparse_point(path):
            # Files and junctions: one disposition-delete on this handle.
            error = root.delete()
            if error is None:
                return None
            # Read-only content refuses the delete; clear the bit and retry
            # against the path the handle resolved to, never the caller's name.
            root.close()
            try:
                os.chmod(resolved, stat.S_IWRITE)
            except OSError:
                return error
            return _delete_one_via_handle(resolved)
        # Directories must be empty before the handle delete succeeds. Children
        # are removed through their own handles, deepest first.
        error = _delete_tree_via_handles(resolved)
        if error:
            return error
        return root.delete()


def _delete_tree_via_handles(root: Path) -> Optional[str]:
    """Empty `root` by deleting each descendant through its own handle."""
    try:
        with os.scandir(root) as it:
            children = list(it)
    except OSError as exc:
        return f"cannot enumerate {root}: {exc}"
    for entry in children:
        child = Path(entry.path)
        try:
            is_dir = entry.is_dir(follow_symlinks=False) and not is_reparse_dirent(entry)
        except OSError:
            is_dir = False
        if is_dir:
            error = _delete_tree_via_handles(child)
            if error:
                return error
        error = _delete_one_via_handle(child)
        if error:
            return error
    return None


def _delete_one_via_handle(target: Path) -> Optional[str]:
    """Delete a single file or empty directory through its own handle.

    A read-only file opens fine but refuses the disposition delete, so the
    read-only bit is cleared and the delete retried once — on the same path we
    already resolved, not a fresh lookup.
    """
    handle, err = open_delete_handle(target)
    if handle is None:
        return err
    with handle:
        error = handle.delete()
        if error is None:
            return None
    try:
        os.chmod(target, stat.S_IWRITE)
    except OSError:
        return error
    retry, err = open_delete_handle(target)
    if retry is None:
        return err
    with retry:
        return retry.delete()


def handle_delete_supported() -> bool:
    return _handle_api() is not None


# ---------- Model ----------
@dataclass
class Item:
    path: Path
    size: int = 0
    partial: int = 0
    scanning: bool = False
    selected: bool = False
    deleted: bool = False
    error: Optional[str] = None
    # True when a sizing budget cut the walk short: `size` is a lower bound.
    truncated: bool = False
    # Allocated bytes — what deleting this actually frees. -1 until measured;
    # measuring costs an extra syscall per file, so it happens for the
    # selection at confirmation time rather than for every scan candidate.
    on_disk: int = -1
    # Logical bytes that live only in the cloud and free nothing locally.
    placeholder_bytes: int = 0
    placeholder_files: int = 0


@dataclass
class Category:
    key: str
    title: str
    description: str
    icon: str = "📁"
    safe_default: bool = False
    items: List[Item] = field(default_factory=list)
    expanded: bool = True
    scanning: bool = True

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items if not i.deleted)

    @property
    def selected_size(self) -> int:
        return sum(i.size for i in self.items if i.selected and not i.deleted)


# ---------- Category catalogs ----------
DEV_FOLDER_CATEGORIES: Dict[str, str] = {
    "node_modules":   "node_modules",
    ".venv":          "Python venvs",
    "venv":           "Python venvs",
    "env":            "Python venvs",
    "__pycache__":    "Python __pycache__",
    ".pytest_cache":  "pytest cache",
    ".mypy_cache":    "mypy cache",
    ".ruff_cache":    "ruff cache",
    ".tox":           "tox envs",
    "target":         "Rust/Java target",
    "build":          "Build outputs",
    "dist":           "Dist outputs",
    "out":            "Build outputs",
    ".next":          "Next.js .next",
    ".nuxt":          "Nuxt .nuxt",
    ".turbo":         "Turbo cache",
    ".parcel-cache":  "Parcel cache",
    ".angular":       "Angular cache",
    ".gradle":        "Gradle project cache",
    ".idea":          "JetBrains .idea",
    ".vs":            "Visual Studio .vs",
    "obj":            ".NET obj/",
}

# (key, title, desc, path, safe_default, icon)
FIXED_PATH_CATEGORIES = [
    ("npm-cache",      "npm cache",           "%APPDATA%\\npm-cache",                  APPDATA / "npm-cache",                                                  True,  "📦"),
    ("pip-cache",      "pip cache",           "%LOCALAPPDATA%\\pip\\Cache",            LOCALAPPDATA / "pip" / "Cache",                                         True,  "🐍"),
    ("yarn-cache",     "Yarn cache",          "%LOCALAPPDATA%\\Yarn\\Cache",           LOCALAPPDATA / "Yarn" / "Cache",                                        True,  "🧶"),
    ("pnpm-store",     "pnpm store",          "%LOCALAPPDATA%\\pnpm\\store",           LOCALAPPDATA / "pnpm" / "store",                                        True,  "📦"),
    ("cargo-cache",    "Cargo cache",         "~\\.cargo\\registry\\cache",            USER / ".cargo" / "registry" / "cache",                                 True,  "🦀"),
    ("gradle-cache",   "Gradle global cache", "~\\.gradle\\caches",                    USER / ".gradle" / "caches",                                            True,  "🐘"),
    ("maven-repo",     "Maven local repo",    "~\\.m2\\repository",                    USER / ".m2" / "repository",                                            False, "☕"),
    ("nuget-cache",    "NuGet packages",      "~\\.nuget\\packages",                   USER / ".nuget" / "packages",                                           False, "🔷"),
    ("go-cache",       "Go build cache",      "~\\AppData\\Local\\go-build",           LOCALAPPDATA / "go-build",                                              True,  "🐹"),
    ("chrome-cache",   "Chrome cache",        "Chrome Default\\Cache",                 LOCALAPPDATA / "Google" / "Chrome" / "User Data" / "Default" / "Cache", True,  "🌐"),
    ("edge-cache",     "Edge cache",          "Edge Default\\Cache",                   LOCALAPPDATA / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache",True,  "🌐"),
    ("user-temp",      "User Temp",           "%TEMP%",                                TEMP,                                                                   True,  "🗑"),
    ("win-temp",       "Windows Temp",        "C:\\Windows\\Temp",                     Path(r"C:\Windows\Temp"),                                               True,  "🗑"),
    ("win-update",     "Windows Update cache","C:\\Windows\\SoftwareDistribution\\Download", Path(r"C:\Windows\SoftwareDistribution\Download"),                False, "🪟"),
    ("crash-dumps",    "Crash dumps",         "%LOCALAPPDATA%\\CrashDumps",            LOCALAPPDATA / "CrashDumps",                                            True,  "💥"),
    # NEW developer-aware locations (mole-style)
    ("jetbrains",      "JetBrains caches",    "%LOCALAPPDATA%\\JetBrains",             LOCALAPPDATA / "JetBrains",                                             True,  "💡"),
    ("vscode-storage", "VS Code workspace storage","%APPDATA%\\Code\\User\\workspaceStorage", APPDATA / "Code" / "User" / "workspaceStorage",                  True,  "🟦"),
    ("vscode-cache",   "VS Code cache",       "%APPDATA%\\Code\\Cache",                APPDATA / "Code" / "Cache",                                             True,  "🟦"),
    ("puppeteer",      "Puppeteer Chromium",  "~\\.cache\\puppeteer",                  USER / ".cache" / "puppeteer",                                          False, "🎭"),
    ("playwright",     "Playwright browsers", "%LOCALAPPDATA%\\ms-playwright",         LOCALAPPDATA / "ms-playwright",                                         False, "🎭"),
    ("docker-desktop", "Docker Desktop data", "%LOCALAPPDATA%\\Docker",                LOCALAPPDATA / "Docker",                                                False, "🐋"),
    ("electron-cache", "Electron cache",      "~\\.electron",                          USER / ".electron",                                                     True,  "⚛"),
    ("conda-pkgs",     "Conda pkgs cache",    "~\\anaconda3\\pkgs",                    USER / "anaconda3" / "pkgs",                                            False, "🅒"),
]


def build_fixed_path_categories() -> List[tuple]:
    """Return Windows cleanup categories, including optional app/browser caches."""
    extra = [
        ("firefox-cache",   "Firefox cache",       "Firefox profile cache2 folders",        LOCALAPPDATA / "Mozilla" / "Firefox" / "Profiles",                    True,  "🌐"),
        ("brave-cache",    "Brave cache",         "Brave Default\\Cache",                 LOCALAPPDATA / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default" / "Cache", True, "🌐"),
        ("opera-cache",    "Opera cache",         "Opera Stable\\Cache",                  APPDATA / "Opera Software" / "Opera Stable" / "Cache",                 True,  "🌐"),
        ("vivaldi-cache",  "Vivaldi cache",       "Vivaldi Default\\Cache",               LOCALAPPDATA / "Vivaldi" / "User Data" / "Default" / "Cache",           True,  "🌐"),
        ("discord-cache",  "Discord cache",       "%APPDATA%\\discord\\Cache",            APPDATA / "discord" / "Cache",                                         True,  "💬"),
        ("slack-cache",    "Slack cache",         "%APPDATA%\\Slack\\Cache",              APPDATA / "Slack" / "Cache",                                           True,  "💬"),
        ("teams-cache",    "Teams cache",         "%APPDATA%\\Microsoft\\Teams\\Cache",    APPDATA / "Microsoft" / "Teams" / "Cache",                            True,  "💬"),
        ("spotify-cache",  "Spotify cache",       "%LOCALAPPDATA%\\Spotify\\Storage",     LOCALAPPDATA / "Spotify" / "Storage",                                  True,  "🎵"),
        ("adobe-cache",    "Adobe media cache",   "%APPDATA%\\Adobe\\Common\\Media Cache", APPDATA / "Adobe" / "Common" / "Media Cache",                         True,  "🎬"),
        ("thumbnail-cache","Windows thumbnail cache","Explorer thumbcache db",             LOCALAPPDATA / "Microsoft" / "Windows" / "Explorer",                   True,  "🖼"),
        ("directx-shader-cache", "DirectX shader cache", "%LOCALAPPDATA%\\D3DSCache",       LOCALAPPDATA / "D3DSCache",                                            True,  "🎮"),
        ("wer-reports",    "Windows Error Reports","%LOCALAPPDATA%\\Microsoft\\Windows\\WER", LOCALAPPDATA / "Microsoft" / "Windows" / "WER",                     True,  "🧾"),
        ("store-cache",    "Microsoft Store cache", "%LOCALAPPDATA%\\Packages\\Microsoft.WindowsStore*", LOCALAPPDATA / "Packages", True, "🏪"),
        ("wsl-logs",       "WSL logs", "%LOCALAPPDATA%\\Packages\\*\\LocalState\\*.log", LOCALAPPDATA / "Packages", False, "🐧"),
        ("delivery-optimization", "Delivery Optimization cache", "Windows delivery cache", Path(r"C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache"), False, "🪟"),
    ]
    return FIXED_PATH_CATEGORIES + extra


def fixed_category_items(key: str, path: Path, selected: bool) -> List[Item]:
    if key == "firefox-cache":
        out: List[Item] = []
        if path_exists(path):
            for profile in path.iterdir():
                cache = profile / "cache2"
                if path_exists(cache):
                    out.append(Item(path=cache, selected=selected))
        return out
    if key == "thumbnail-cache":
        out = []
        if path_exists(path):
            for pattern in ("thumbcache_*.db", "iconcache_*.db"):
                for p in path.glob(pattern):
                    out.append(Item(path=p, selected=selected))
        return out
    if key == "docker-desktop":
        out = []
        if path_exists(path):
            for child in path.iterdir():
                name = child.name.lower()
                if any(k in name for k in ("log", "cache", "tmp")) and not is_protected_path(child):
                    out.append(Item(path=child, selected=selected))
        return out
    if key == "store-cache":
        out = []
        if path_exists(path):
            for pkg in path.glob("Microsoft.WindowsStore*"):
                for rel in ("LocalCache", "Cache", "TempState"):
                    p = pkg / rel
                    if path_exists(p):
                        out.append(Item(path=p, selected=selected))
        return out
    if key == "wsl-logs":
        out = []
        if path_exists(path):
            for pkg in path.glob("MicrosoftCorporationII.WindowsSubsystemForLinux*"):
                ls = pkg / "LocalState"
                if not path_exists(ls):
                    continue
                for pattern in ("*.log", "*.txt"):
                    for p in ls.glob(pattern):
                        out.append(Item(path=p, selected=False))
        return out
    return [Item(path=path, selected=selected)] if path_exists(path) else []

DEV_CATEGORY_ICONS: Dict[str, str] = {
    "node_modules":         "📚",
    "Python venvs":         "🐍",
    "Python __pycache__":   "🐍",
    "pytest cache":         "🧪",
    "mypy cache":           "🧪",
    "ruff cache":           "🧪",
    "tox envs":             "🧪",
    "Rust/Java target":     "🛠",
    "Build outputs":        "🛠",
    "Dist outputs":         "📤",
    "Next.js .next":        "▲",
    "Nuxt .nuxt":           "💚",
    "Turbo cache":          "⚡",
    "Parcel cache":         "📦",
    "Angular cache":        "🅰",
    "Gradle project cache": "🐘",
    "JetBrains .idea":      "💡",
    "Visual Studio .vs":    "🟦",
    ".NET obj/":            "🟦",
}

SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "Windows", "ProgramData",
             "Program Files", "Program Files (x86)", "AppData",
             # Never a cleanup target, and object stores make it a large share
             # of the walk cost across a directory full of repositories.
             ".git"}

INSTALLER_EXTS = {".exe", ".msi", ".iso", ".zip", ".7z", ".dmg", ".pkg"}


def discover_scan_roots() -> List[Path]:
    candidates = [
        USER / "Projects", USER / "projects",
        USER / "source", USER / "Source", USER / "src",
        USER / "repos", USER / "Repos",
        USER / "Documents", USER / "Desktop",
        USER / "Downloads",
        USER / "Code", USER / "code",
        USER / "dev", USER / "Dev",
        USER / "workspace", USER / "Workspace",
        USER / "git",
    ]
    seen, roots = set(), []
    for c in candidates:
        if c.exists() and c not in seen:
            roots.append(c)
            seen.add(c)
    for letter in "DEFGH":
        p = Path(f"{letter}:\\")
        if p.exists():
            roots.append(p)
    return roots


def load_purge_roots(home: Path = USER) -> List[Path]:
    paths_file = home / ".wmole" / "purge_paths.txt"
    roots: List[Path] = []
    if not path_exists(paths_file):
        return roots
    try:
        for line in paths_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            expanded = os.path.expandvars(line)
            if expanded.startswith("~"):
                expanded = str(home) + expanded[1:]
            p = Path(expanded)
            if p.exists() and p not in roots:
                roots.append(p)
    except OSError:
        return []
    return roots


WMOLERC_NAME = ".wmolerc"
_PROJECT_RC_CACHE: Dict[str, Optional[dict]] = {}


def load_project_rc(root: Path) -> Optional[dict]:
    """Read `.wmolerc` (JSON) from a project root. None when missing/broken.

    Shape: {"purge": ["dist"], "keep": ["packages/*/dist"], "max_depth": 4}
    """
    rc_file = root / WMOLERC_NAME
    if not path_exists(rc_file):
        return None
    try:
        data = json.loads(rc_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _debug_log(f"wmolerc: unreadable {rc_file}: {exc}")
        return None
    if not isinstance(data, dict):
        _debug_log(f"wmolerc: not an object {rc_file}")
        return None
    purge = [str(v) for v in data.get("purge", []) if isinstance(v, (str, int))] \
        if isinstance(data.get("purge"), list) else []
    keep = [str(v) for v in data.get("keep", []) if isinstance(v, (str, int))] \
        if isinstance(data.get("keep"), list) else []
    depth = data.get("max_depth")
    if not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0:
        depth = 0
    return {"root": str(root), "purge": purge, "keep": keep, "max_depth": depth}


def find_project_rc(path: Path) -> Optional[dict]:
    """Nearest `.wmolerc` at or above `path` (directories only)."""
    try:
        start = path if path.is_dir() else path.parent
    except OSError:
        start = path
    for parent in [start, *start.parents]:
        key = str(parent)
        if key in _PROJECT_RC_CACHE:
            rc = _PROJECT_RC_CACHE[key]
        else:
            rc = load_project_rc(parent)
            _PROJECT_RC_CACHE[key] = rc
        if rc is not None:
            return rc
    return None


def _rc_relative(root: Path, path: Path) -> Optional[str]:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def rc_is_kept(rc: dict, path: Path) -> bool:
    """True when a `keep` glob covers `path` or one of its parents (whitelist-like)."""
    rel = _rc_relative(Path(rc.get("root", "")), path)
    if rel is None:
        return False
    parts = rel.lower().split("/")
    for raw in rc.get("keep", []):
        pattern = str(raw).replace("\\", "/").strip("/").lower()
        if not pattern:
            continue
        for i in range(len(parts)):
            if fnmatch.fnmatch("/".join(parts[:i + 1]), pattern):
                return True
    return False


def rc_allows_purge(rc: Optional[dict], path: Path) -> bool:
    """Apply a project `.wmolerc` to a purge candidate."""
    if not rc:
        return True
    rel = _rc_relative(Path(rc.get("root", "")), path)
    if rel is None:
        return True
    if rc_is_kept(rc, path):
        return False
    purge = [str(v).lower() for v in rc.get("purge", [])]
    if purge and path.name.lower() not in purge:
        return False
    depth = int(rc.get("max_depth", 0) or 0)
    if depth and len(rel.split("/")) > depth:
        return False
    return True


def rc_allows_path(path: Path) -> bool:
    """Convenience wrapper: resolve the nearest `.wmolerc` and evaluate it."""
    return rc_allows_purge(find_project_rc(path), path)


def iter_dev_folders(roots: List[Path]):
    for root in roots:
        for dirpath, dirnames, _ in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("$")]
            prune_reparse_dirs(dirpath, dirnames)
            matched_here = []
            for d in list(dirnames):
                title = DEV_FOLDER_CATEGORIES.get(d)
                if title:
                    yield title, Path(dirpath) / d
                    matched_here.append(d)
            for d in matched_here:
                try:
                    dirnames.remove(d)
                except ValueError:
                    pass


def _file_count_and_size(path: Path) -> tuple[int, int]:
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 0, 0
    count = 0
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        prune_reparse_dirs(root, dirs)
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
                count += 1
            except OSError:
                pass
    return count, total


def find_large_files(path: Path, minimum_size: int, on_progress=None) -> List[dict]:
    large: List[dict] = []
    if path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size >= minimum_size:
            large.append({"name": path.name, "path": str(path), "size": size})
        return large
    scanned = 0
    last_emit = time.time()
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        prune_reparse_dirs(root, dirs)
        for name in files:
            p = Path(root) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            scanned += 1
            if size >= minimum_size:
                large.append({"name": p.name, "path": str(p), "size": size})
            # Emit progress every 0.15s for live feedback
            if on_progress and time.time() - last_emit > 0.15:
                on_progress(scanned, len(large))
                last_emit = time.time()
        if on_progress and time.time() - last_emit > 0.3:
            on_progress(scanned, len(large))
            last_emit = time.time()
    if on_progress:
        on_progress(scanned, len(large))
    large.sort(key=lambda row: row["size"], reverse=True)
    return large


def analyze_path(path: Path, large_file_min: int = 1024 ** 3) -> dict:
    path = path.expanduser()
    entries: List[dict] = []
    if path.is_dir():
        for child in path.iterdir():
            count, size = _file_count_and_size(child)
            entries.append({
                "name": child.name,
                "path": str(child),
                "size": size,
                "is_dir": child.is_dir(),
                "total_files": count,
            })
    elif path.exists():
        count, size = _file_count_and_size(path)
        entries.append({
            "name": path.name,
            "path": str(path),
            "size": size,
            "is_dir": False,
            "total_files": count,
        })
    entries.sort(key=lambda row: row["size"], reverse=True)
    total_files, total_size = _file_count_and_size(path)
    return {
        "path": str(path),
        "entries": entries,
        "large_files": find_large_files(path, large_file_min),
        "total_size": total_size,
        "total_files": total_files,
    }


def quick_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    return dir_size(path, max_seconds=0.2, max_files=2500)


def build_fs_category(path: Path, live=None, scanner=None, view=None, cursor=0,
                       use_trash=True, dry_run=False, apps=None, opt=None) -> Category:
    path = path.expanduser()
    title = str(path)
    cat = Category(
        key=f"fs:{path}",
        title=title,
        description="filesystem explorer",
        icon="📁",
        safe_default=False,
        scanning=True,
    )
    if path.parent != path:
        up = Item(path=path.parent, size=0, selected=False)
        up.error = "up"
        cat.items.append(up)
    if path_exists(path):
        try:
            children = list(path.iterdir())
        except OSError:
            children = []
        rows: List[Item] = []
        total_children = len(children)
        for idx, child in enumerate(children):
            if is_protected_path(child):
                continue
            rows.append(Item(path=child, size=quick_size(child), selected=False))
            # Show progress for large directories
            if live is not None and total_children > 50 and idx % 10 == 0:
                filled = int(20 * idx / total_children) if total_children > 0 else 20
                progress_text = Text()
                progress_text.append("[" + "█" * filled + "·" * (20 - filled) + "]", style="bright_cyan")
                progress_text.append(f"  Reading {idx + 1}/{total_children} items…", style="white")
                live.update(Group(
                    render(scanner, view, cursor, "", use_trash, dry_run, apps or [], opt or []),
                    Panel(progress_text, title="Opening directory…", border_style="cyan", padding=(1, 2)),
                ))
        rows.sort(key=lambda it: (not it.path.is_dir(), -it.size, it.path.name.lower()))
        cat.items.extend(rows)
    cat.scanning = False
    return cat


def build_drive_picker_category() -> Category:
    cat = Category(
        key="fs:drives",
        title="Drives",
        description="select a drive",
        icon="💽",
        safe_default=False,
        scanning=False,
    )
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        p = Path(f"{letter}:\\")
        if not path_exists(p):
            continue
        try:
            usage = shutil.disk_usage(p)
            size = usage.total
        except OSError:
            size = 0
        cat.items.append(Item(path=p, size=size, selected=False))
    cat.items.sort(key=lambda it: it.path.drive)
    return cat


def _streaming_map(pool: ThreadPoolExecutor, fn, items, window: int, cancel=None):
    """Like Executor.map, but pulls from `items` lazily.

    Executor.map submits every item before yielding anything, so a generator
    source is fully drained first. This keeps at most `window` jobs in flight
    and yields results in completion order."""
    pending: list = []
    source = iter(items)
    exhausted = False
    while True:
        while not exhausted and len(pending) < max(1, window):
            if cancel is not None and cancel.is_set():
                exhausted = True
                break
            try:
                pending.append(pool.submit(fn, next(source)))
            except StopIteration:
                exhausted = True
        if not pending:
            return
        done, still = _futures.wait(pending, return_when=_futures.FIRST_COMPLETED)
        pending = list(still)
        for fut in done:
            try:
                yield fut.result()
            except Exception as exc:  # a single unreadable dir must not abort
                _debug_log(f"_streaming_map: {exc}")


def iter_purge_candidates(roots: List[Path], scan_filter: "ScanFilter",
                          workers: int = 4, cancel=None):
    """Yield (title, path) purge candidates as they are found.

    Roots are walked concurrently and results are streamed, so sizing can start
    on the first candidate instead of waiting for the whole discovery pass
    (measured at 97s across this machine's six default roots)."""
    queue: "_queue.Queue" = _queue.Queue(maxsize=512)
    done = object()

    def walk_one(root: Path) -> None:
        try:
            for title, p in iter_dev_folders([root]):
                if cancel is not None and cancel.is_set():
                    return
                if scan_filter.rejects(p) or not rc_allows_path(p):
                    continue
                queue.put((title, p))
        except Exception as exc:  # one bad root must not kill the scan
            _debug_log(f"iter_purge_candidates: {root}: {exc}")

    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(roots) or 1)))
    futures = [pool.submit(walk_one, r) for r in roots]

    def close() -> None:
        for f in futures:
            f.exception()
        queue.put(done)

    threading.Thread(target=close, daemon=True).start()
    try:
        while True:
            entry = queue.get()
            if entry is done:
                return
            yield entry
    finally:
        pool.shutdown(wait=False)


def build_purge_categories(roots: List[Path], whitelist: Optional[List[Path]] = None,
                           cache: Optional[dict] = None,
                           workers: Optional[int] = None,
                           on_item=None, on_progress=None, cancel=None,
                           dir_budget: Optional[float] = PURGE_DIR_BUDGET_SECONDS) -> List[Category]:
    """Discover and size project build artifacts under `roots`.

    Discovery and sizing are pipelined: each candidate is sized as soon as it is
    found. `on_item(title, item)` and `on_progress(done, label)` fire as results
    land so callers can stream them, and `cancel` is honoured between items
    rather than only at the end.
    """
    workers = workers or min(8, (os.cpu_count() or 4) * 2)
    scan_filter = ScanFilter(whitelist)
    roots = [ScanFilter.resolve_root(r) for r in roots]
    cache_lock = threading.Lock()
    by_title: Dict[str, Category] = {}
    done = 0

    def size_one(pair):
        title, p = pair
        if cancel is not None and cancel.is_set():
            # Already-queued jobs return immediately so a cancel is not held up
            # by the full window of pending sizing work.
            return title, Item(path=p, size=0, selected=False, truncated=True)
        size, truncated = sized_dir(p, cache, cache_lock, max_seconds=dir_budget)
        return title, Item(path=p, size=size, selected=True, truncated=truncated)

    candidates = iter_purge_candidates(roots, scan_filter, workers=workers, cancel=cancel)
    # NOT Executor.map: it materialises the whole iterable up front, which would
    # re-serialise discovery ahead of sizing. Keep a bounded window of in-flight
    # sizing jobs instead so both phases overlap.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for title, item in _streaming_map(pool, size_one, candidates,
                                          window=workers * 2, cancel=cancel):
            if cancel is not None and cancel.is_set():
                break
            cat = by_title.setdefault(title, Category(
                key=f"dev-{title}",
                title=title,
                description="project build artifacts",
                icon=DEV_CATEGORY_ICONS.get(title, "📦"),
                safe_default=True,
                scanning=False,
            ))
            cat.items.append(item)
            done += 1
            if on_item:
                on_item(cat, item)
            if on_progress:
                on_progress(done, str(item.path))

    categories = list(by_title.values())
    for cat in categories:
        cat.items.sort(key=lambda item: item.size, reverse=True)
        cat.description = f"{len(cat.items)} artifact(s)"
    categories.sort(key=lambda cat: cat.total, reverse=True)
    return categories


def build_installer_categories(roots: List[Path], days: int = 30) -> List[Category]:
    cutoff = time.time() - days * 86400
    items: List[Item] = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("$")]
            prune_reparse_dirs(dirpath, dirnames)
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in INSTALLER_EXTS:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if days <= 0 or st.st_mtime <= cutoff:
                    items.append(Item(path=p, size=st.st_size, selected=True))
    items.sort(key=lambda item: item.size, reverse=True)
    if not items:
        return []
    return [Category(
        key="installers",
        title="Installers",
        description=f"installer files older than {days} day(s)",
        icon="💿",
        safe_default=True,
        items=items,
        scanning=False,
    )]


def build_clean_path_categories(roots: List[Path], days: int = 90) -> List[Category]:
    cutoff = time.time() - days * 86400
    old_items: List[Item] = []
    for root in roots:
        if not path_exists(root):
            continue
        try:
            children = list(root.iterdir()) if root.is_dir() else [root]
        except OSError:
            continue
        for child in children:
            if is_protected_path(child):
                continue
            rc = find_project_rc(child)
            if rc and rc_is_kept(rc, child):
                continue
            try:
                if child.stat().st_mtime <= cutoff:
                    size = dir_size(child) if child.is_dir() else child.stat().st_size
                    old_items.append(Item(path=child, size=size, selected=True))
            except OSError:
                pass
    cats = build_installer_categories(roots, days=0)
    if old_items:
        old_items.sort(key=lambda item: item.size, reverse=True)
        cats.append(Category(
            key="old-files",
            title=f"Old files ({days}d+)",
            description="old files under explicit clean paths",
            icon="📥",
            safe_default=True,
            items=old_items,
            scanning=False,
        ))
    cats.sort(key=lambda cat: cat.total, reverse=True)
    return cats


def collect_selected_targets(categories: List[Category], estimated: bool = False) -> dict:
    targets = []
    blocked = []
    for cat in categories:
        for item in cat.items:
            if not item.selected or item.deleted:
                continue
            row = {"path": str(item.path), "size": item.size}
            if is_protected_path(item.path):
                row["reason"] = "protected path"
                blocked.append(row)
            else:
                targets.append(row)
    return {
        "targets": targets,
        "blocked": blocked,
        "total": sum(row["size"] for row in targets),
        "estimated": estimated,
    }


def measure_selection(items: List["Item"], max_seconds_each: Optional[float] = 8.0) -> DiskFacts:
    """Fill in on-disk and placeholder figures for the items about to be deleted.

    Mutates each Item so the UI can show per-row detail, and returns the
    aggregate. `size` (logical) is left untouched — the two numbers are
    reported side by side rather than one silently replacing the other.
    """
    total = DiskFacts()
    for it in items:
        facts = disk_facts(it.path, max_seconds=max_seconds_each)
        it.on_disk = facts.on_disk
        it.placeholder_bytes = facts.placeholder_bytes
        it.placeholder_files = facts.placeholder_files
        total.logical += facts.logical
        total.on_disk += facts.on_disk
        total.placeholder_bytes += facts.placeholder_bytes
        total.placeholder_files += facts.placeholder_files
        total.files += facts.files
        total.truncated = total.truncated or facts.truncated
    return total


def discover_game_dirs() -> List[Item]:
    out: List[Item] = []
    bases = [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common"),
        Path(r"C:\Program Files\Steam\steamapps\common"),
        Path(r"C:\Program Files (x86)\Epic Games"),
        Path(r"C:\Program Files\Epic Games"),
        Path(r"C:\Program Files (x86)\GOG Galaxy\Games"),
        Path(r"C:\XboxGames"),
        Path(r"C:\Riot Games"),
    ]
    for letter in "DEFGH":
        for guess in (f"{letter}:\\SteamLibrary\\steamapps\\common", f"{letter}:\\Steam\\steamapps\\common"):
            bases.append(Path(guess))
    for base in bases:
        if base.exists():
            try:
                for child in base.iterdir():
                    if child.is_dir():
                        out.append(Item(path=child))
            except OSError:
                pass
    return out


def recycle_bin_items() -> List[Item]:
    out: List[Item] = []
    for letter in "CDEFGH":
        p = Path(f"{letter}:\\$Recycle.Bin")
        if p.exists():
            out.append(Item(path=p))
    return out


def old_downloads(days: int = 90) -> List[Item]:
    d = USER / "Downloads"
    if not d.exists():
        return []
    cutoff = time.time() - days * 86400
    out: List[Item] = []
    try:
        for child in d.iterdir():
            try:
                if child.stat().st_mtime < cutoff:
                    out.append(Item(path=child))
            except OSError:
                pass
    except OSError:
        pass
    return out


def old_installers(days: int = 30) -> List[Item]:
    out: List[Item] = []
    cutoff = time.time() - days * 86400
    for d in [USER / "Downloads", USER / "Desktop"]:
        if not d.exists():
            continue
        for root, dirs, files in os.walk(d, onerror=lambda e: None):
            prune_reparse_dirs(root, dirs)
            for f in files:
                if Path(f).suffix.lower() in INSTALLER_EXTS:
                    p = Path(root) / f
                    try:
                        if p.stat().st_mtime < cutoff:
                            out.append(Item(path=p, size=p.stat().st_size))
                    except OSError:
                        pass
    return out


# ---------- Scanner ----------
class Scanner:
    def __init__(self, whitelist: Optional[List[Path]] = None,
                 profile: str = "full", roots: Optional[List[Path]] = None,
                 use_cache: bool = True, on_item=None, on_progress=None,
                 cancel=None) -> None:
        # on_item(category, item) / on_progress(done, label) let a caller stream
        # results while the scan runs; `cancel` is a threading.Event checked
        # between items so a long scan can actually be stopped.
        self.on_item = on_item
        self.on_progress = on_progress
        self.cancel = cancel
        self.categories: List[Category] = []
        self.status: str = "Initializing…"
        self.done: bool = False
        self.current_cat_key: Optional[str] = None
        self.current_item_id: Optional[int] = None
        self.whitelist = whitelist or []
        self.profile = profile
        self.roots = roots
        self._lock = threading.Lock()
        self.workers = int(os.environ.get("WMOLE_SCAN_WORKERS",
                                          min(8, (os.cpu_count() or 4) * 2)))
        self.use_cache = use_cache
        self.cache = load_size_cache() if use_cache else {}
        self._cache_dirty = False

    def _add(self, c: Category) -> None:
        with self._lock:
            self.categories.append(c)

    def _size_item(self, it: Item) -> None:
        if is_whitelisted(it.path, self.whitelist):
            it.size = 0
            it.scanning = False
            it.error = "whitelisted"
            it.selected = False
            return
        try:
            mtime = it.path.stat().st_mtime
        except OSError:
            mtime = None
        if self.use_cache and mtime is not None:
            with self._lock:
                cached = cache_get(self.cache, it.path, mtime)
            if cached is not None:
                it.size = cached
                it.partial = cached
                it.scanning = False
                return
        it.scanning = True
        with self._lock:
            self.current_item_id = id(it)
        def on_progress(n: int) -> None:
            it.partial = n
        if self.profile == "clean":
            it.size = dir_size(it.path, on_progress=on_progress, max_seconds=0.35, max_files=5000)
        else:
            it.size = dir_size(it.path, on_progress=on_progress)
        it.partial = it.size
        it.scanning = False
        with self._lock:
            self.current_item_id = None
            if self.use_cache and mtime is not None:
                cache_set(self.cache, it.path, mtime, it.size)
                self._cache_dirty = True

    def run(self) -> None:
        if self.profile == "idle":
            self.status = "Idle."
            self.done = True
            return
        if self.profile == "clean" and self.roots:
            for cat in build_clean_path_categories(self.roots):
                self._add(cat)
            self.status = "Clean path scan complete."
            self.done = True
            return
        if self.profile == "purge":
            def note(n: int, label: str) -> None:
                self.status = f"Sized {n} artifact folder(s) — {label}"
                if self.on_progress:
                    self.on_progress(n, label)
            for cat in build_purge_categories(self.roots or load_purge_roots() or discover_scan_roots(),
                                              self.whitelist,
                                              cache=self.cache if self.use_cache else None,
                                              workers=self.workers,
                                              on_item=self.on_item,
                                              on_progress=note,
                                              cancel=self.cancel):
                self._add(cat)
            if self.use_cache:
                save_size_cache(self.cache)
            self.status = "Purge scan complete."
            self.done = True
            return
        if self.profile in ("installer", "installers"):
            for cat in build_installer_categories(self.roots or [USER / "Downloads", USER / "Desktop"]):
                self._add(cat)
            self.status = "Installer scan complete."
            self.done = True
            return

        fixed_cats: List[Category] = []
        for key, title, desc, path, safe, icon in build_fixed_path_categories():
            cat = Category(key=key, title=title, description=desc, icon=icon,
                           safe_default=safe, expanded=False)
            cat.items.extend(fixed_category_items(key, path, selected=safe))
            self._add(cat)
            fixed_cats.append(cat)

        rb = Category(key="recyclebin", title="Recycle Bin", description="all drives $Recycle.Bin",
                      icon="🗑", safe_default=True, expanded=False)
        rb.items = recycle_bin_items()
        for it in rb.items:
            it.selected = True
        self._add(rb)
        fixed_cats.append(rb)

        od = Category(key="old-downloads", title="Old Downloads (90d+)",
                      description="files in ~\\Downloads older than 90 days",
                      icon="📥", expanded=False)
        od.items = old_downloads(90)
        self._add(od)
        fixed_cats.append(od)

        inst = Category(key="installers", title="Old installers (30d+)",
                        description=".exe/.msi/.iso/.zip in Downloads & Desktop older than 30 days",
                        icon="💿", expanded=False)
        inst.items = old_installers(30)
        self._add(inst)
        fixed_cats.append(inst)

        if self.profile != "clean":
            games = Category(key="games", title="Games & Launchers",
                         description="Steam / Epic / GOG / Xbox installs",
                         icon="🎮", expanded=True)
            games.items = discover_game_dirs()
            self._add(games)
            fixed_cats.append(games)

        dev_titles: Dict[str, Category] = {}
        if self.profile != "clean":
            for title in sorted({t for t in DEV_FOLDER_CATEGORIES.values() if t}):
                cat = Category(key=f"dev-{title}", title=title,
                           description="(searching dev dirs…)",
                           icon=DEV_CATEGORY_ICONS.get(title, "📦"),
                               safe_default=True, expanded=False)
                dev_titles[title] = cat
                self._add(cat)

        pending = [it for cat in fixed_cats for it in cat.items if it.size == 0]
        self.status = f"Scanning {len(fixed_cats)} categories ({len(pending)} items)…"
        if pending:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(self._size_item, pending))
        for cat in fixed_cats:
            cat.scanning = False
        self.current_cat_key = None

        if self.profile == "clean":
            if self._cache_dirty:
                save_size_cache(self.cache)
            with self._lock:
                self.categories.sort(key=lambda c: c.total, reverse=True)
            self.status = "Clean scan complete."
            self.done = True
            return

        roots = self.roots or discover_scan_roots()
        self.status = f"Searching {len(roots)} root(s) for dev folders…"
        dev_items: List[Item] = []
        for title, p in iter_dev_folders(roots):
            cat = dev_titles[title]
            it = Item(path=p, selected=True)
            with self._lock:
                cat.items.append(it)
            dev_items.append(it)
        self.status = f"Sizing {len(dev_items)} dev folder(s)…"
        if dev_items:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(self._size_item, dev_items))
        for title, cat in dev_titles.items():
            cat.items.sort(key=lambda i: i.size, reverse=True)
            cat.description = f"{len(cat.items)} folder(s) under your dev dirs"
        self.current_cat_key = None

        for cat in dev_titles.values():
            cat.scanning = False

        if self._cache_dirty:
            save_size_cache(self.cache)
        with self._lock:
            self.categories.sort(key=lambda c: c.total, reverse=True)
        self.status = "Scan complete."
        self.done = True


# ---------- Status / health (mode: status) ----------
def health_score() -> int:
    """0-100 — higher is better."""
    if psutil is None:
        return 50
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory().percent
    du = psutil.disk_usage(USER.anchor or "C:\\").percent
    # lower load = higher score
    score = 100 - int((cpu * 0.3 + mem * 0.3 + du * 0.4))
    return max(0, min(100, score))


def render_status() -> Group:
    if psutil is None:
        return Group(Text("psutil not installed — `pip install psutil`", style="red"))
    cpu = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(USER.anchor or "C:\\")
    net = psutil.net_io_counters()
    dio = psutil.disk_io_counters()
    boot = psutil.boot_time()
    procs = sorted(psutil.process_iter(["name", "cpu_percent", "memory_info"]),
                   key=lambda p: (p.info.get("cpu_percent") or 0), reverse=True)[:8]
    score = health_score()
    score_color = ("green3" if score >= 75 else
                   "yellow" if score >= 50 else
                   "dark_orange" if score >= 30 else "red")

    now = time.time()
    if not hasattr(render_status, "_last"):
        render_status._last = {"t": now, "disk_read": dio.read_bytes if dio else 0, "disk_write": dio.write_bytes if dio else 0, "net_sent": net.bytes_sent, "net_recv": net.bytes_recv}  # type: ignore[attr-defined]
    last = render_status._last  # type: ignore[attr-defined]
    dt = max(0.001, now - last["t"])
    disk_read_rate = ((dio.read_bytes if dio else 0) - last["disk_read"]) / dt
    disk_write_rate = ((dio.write_bytes if dio else 0) - last["disk_write"]) / dt
    net_up_rate = (net.bytes_sent - last["net_sent"]) / dt
    net_down_rate = (net.bytes_recv - last["net_recv"]) / dt
    render_status._last = {"t": now, "disk_read": dio.read_bytes if dio else 0, "disk_write": dio.write_bytes if dio else 0, "net_sent": net.bytes_sent, "net_recv": net.bytes_recv}  # type: ignore[attr-defined]

    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    uptime_s = int(now - boot)
    uptime = f"{uptime_s // 86400}d {(uptime_s % 86400) // 3600}h {(uptime_s % 3600) // 60}m"
    sysinfo = platform.uname()
    temps = {}
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures() or {}
        except Exception:
            temps = {}

    out = Text()
    out.append("System Status\n", style="bold magenta")
    out.append(f"  Health  ", style="grey70")
    out.append(f"{score}/100\n", style=f"bold {score_color}")
    out.append(f"  Device  {sysinfo.system} {sysinfo.release}  ({sysinfo.node})\n", style="grey70")
    out.append(f"  Uptime  {uptime}\n", style="grey70")
    if bat is not None:
        charge = "charging" if bat.power_plugged else "battery"
        out.append(f"  Power   {bat.percent:5.1f}% ({charge})\n", style="grey70")
    out.append(f"  CPU     {cpu:5.1f}%   ", style="grey70")
    out.append(" ".join(f"{c:4.0f}" for c in per_core[:8]), style="bright_cyan")
    out.append("\n")
    out.append(f"  Memory  {mem.percent:5.1f}%  ({human_size(mem.used)} / {human_size(mem.total)})\n", style="grey70")
    out.append(f"  Swap    {swap.percent:5.1f}%  ({human_size(swap.used)} / {human_size(swap.total)})\n", style="grey70")
    out.append(f"  Disk    {disk.percent:5.1f}%  ({human_size(disk.used)} / {human_size(disk.total)}, {human_size(disk.free)} free)\n", style="grey70")
    out.append(f"  Disk I/O ↑{human_size(int(disk_write_rate))}/s  ↓{human_size(int(disk_read_rate))}/s\n", style="grey70")
    out.append(f"  Net     ↑{human_size(net.bytes_sent)} ({human_size(int(net_up_rate))}/s)   ↓{human_size(net.bytes_recv)} ({human_size(int(net_down_rate))}/s)\n", style="grey70")
    if temps:
        sensor_names = []
        for group in temps.values():
            for item in group[:2]:
                if getattr(item, "current", None) is not None:
                    sensor_names.append(f"{item.label or 'temp'} {item.current:.0f}C")
        if sensor_names:
            out.append("  Temp    " + ", ".join(sensor_names[:3]) + "\n", style="grey70")
    out.append("\n  Top processes by CPU:\n", style="bold grey85")
    for p in procs:
        try:
            name = (p.info.get("name") or "?")[:24]
            cpup = p.info.get("cpu_percent") or 0
            rss = (p.info.get("memory_info").rss if p.info.get("memory_info") else 0)
            out.append(f"   {name:<24} {cpup:5.1f}%   {human_size(rss):>10}\n", style="white")
        except Exception:
            pass
    return Group(out)


def read_cleanup_history(limit: int = 6) -> dict:
    """Parse the operations log for completed deletions.

    Returns total bytes freed all-time, op count, last cleanup timestamp, and
    the most recent entries (newest first)."""
    total_freed = 0
    count = 0
    last_ts = ""
    recent: List[dict] = []
    try:
        if path_exists(OP_LOG_FILE):
            lines = OP_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines:
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                ts, action, size_s, target, result = parts[0], parts[1], parts[2], parts[3], parts[4]
                # Count only successful real deletions (skip dry-run/blocked).
                if action not in ("delete-trash", "delete-permanent") or result != "ok":
                    continue
                try:
                    size = int(size_s)
                except ValueError:
                    size = 0
                total_freed += size
                count += 1
                last_ts = ts
                recent.append({"ts": ts, "size": size, "path": target, "action": action})
    except Exception:
        pass
    recent = recent[-limit:][::-1]
    return {"total_freed": total_freed, "count": count, "last_ts": last_ts, "recent": recent}


def _meter_bar(pct: float, width: int = 28, color: str = "bright_cyan") -> Text:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100))
    t = Text()
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="grey30")
    return t


def render_dashboard(scanner: Optional["Scanner"] = None,
                     max_rows: Optional[int] = None) -> Group:
    """Landing dashboard: disk/PC fullness, system health, cleanup history,
    and reclaimable-space estimate from the live clean scan.

    ``max_rows`` is the vertical budget (terminal rows) reserved for this body.
    When the terminal is short, the cleanup-history list is trimmed so the
    footer and the command-input box below always stay visible (otherwise the
    Live screen crops the input box off the bottom)."""
    tr = LANG == "tr"
    out = Text()
    out.append(("📊 Gösterge Paneli\n\n" if tr else "📊 Dashboard\n\n"), style="bold bright_magenta")

    # ── Disk fullness ──
    if psutil is not None:
        try:
            disk = psutil.disk_usage(USER.anchor or "C:\\")
            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=None)
            score = health_score()
            score_color = ("green3" if score >= 75 else "yellow" if score >= 50
                           else "dark_orange" if score >= 30 else "red")
            disk_color = ("red" if disk.percent >= 90 else "dark_orange" if disk.percent >= 75
                          else "bright_cyan")
            mem_color = ("red" if mem.percent >= 90 else "dark_orange" if mem.percent >= 75
                         else "green3")

            out.append("  Disk    ", style="grey70")
            out.append(_meter_bar(disk.percent, color=disk_color))
            out.append(f"  {disk.percent:5.1f}%", style=f"bold {disk_color}")
            out.append(f"   {human_size(disk.used)} / {human_size(disk.total)}  ", style="grey62")
            out.append((f"({human_size(disk.free)} boş)\n" if tr else f"({human_size(disk.free)} free)\n"), style="green")

            out.append("  RAM     ", style="grey70")
            out.append(_meter_bar(mem.percent, color=mem_color))
            out.append(f"  {mem.percent:5.1f}%", style=f"bold {mem_color}")
            out.append(f"   {human_size(mem.used)} / {human_size(mem.total)}\n", style="grey62")

            out.append("  CPU     ", style="grey70")
            out.append(_meter_bar(cpu, color="bright_blue"))
            out.append(f"  {cpu:5.1f}%\n", style="bold bright_blue")

            out.append(("  Sağlık  " if tr else "  Health  "), style="grey70")
            out.append(_meter_bar(score, color=score_color))
            out.append(f"  {score}/100\n", style=f"bold {score_color}")
        except Exception:
            out.append(("  (sistem metrikleri okunamadı)\n" if tr else "  (could not read system metrics)\n"), style="red")
    else:
        out.append(("  psutil yüklü değil — `pip install psutil`\n" if tr else "  psutil not installed — `pip install psutil`\n"), style="red")

    # ── Reclaimable space from the live clean scan ──
    out.append(("\n  Temizlik Analizi\n" if tr else "\n  Cleanup Analysis\n"), style="bold grey85")
    reclaimable = 0
    items = 0
    scan_done = True
    try:
        scan_done = bool(scanner.done) if scanner is not None else True
        cats = list(scanner.categories) if scanner is not None else []
        for c in cats:
            for it in c.items:
                if it.selected and not it.deleted and not is_protected_path(it.path):
                    reclaimable += (it.partial or it.size)
                    items += 1
    except TypeError:
        cats = []
    if scanner is not None:
        if not scan_done:
            out.append(("   ⟳ taranıyor…  " if tr else "   ⟳ scanning…  "), style="bright_yellow")
            out.append((f"şimdiye dek {human_size(reclaimable)} geri kazanılabilir\n" if tr
                        else f"{human_size(reclaimable)} reclaimable so far\n"), style="grey70")
        elif reclaimable > 0:
            out.append(("   ♻ Geri kazanılabilir: " if tr else "   ♻ Reclaimable: "), style="grey70")
            out.append(f"{human_size(reclaimable)}", style="bold bright_green")
            out.append((f"  ({items} öğe)\n" if tr else f"  ({items} items)\n"), style="grey62")
            out.append(("   [C] Hızlı temizliği başlat\n" if tr else "   [C] Start quick clean\n"), style="bright_cyan")
        else:
            out.append(("   ✓ Temizlenecek fazla veri yok\n" if tr else "   ✓ Nothing significant to clean\n"), style="green")
    else:
        out.append(("   (analiz bekleniyor)\n" if tr else "   (analysis pending)\n"), style="grey50")

    # ── Cleanup history ──
    # Everything above already consumed a fixed number of rows; budget the
    # remaining space so the history list never pushes the input box off-screen.
    # Rows used so far + the 3 trailing rows (blank + action line) below.
    rows_so_far = out.plain.count("\n") + 1
    hist_limit = 5
    if max_rows is not None:
        # 2 header/summary rows + 3 trailing rows are non-history overhead here.
        budget = max_rows - rows_so_far - 2 - 3
        hist_limit = max(0, min(5, budget))
    hist = read_cleanup_history(limit=hist_limit)
    out.append(("\n  Yapılan Temizlikler\n" if tr else "\n  Cleanup History\n"), style="bold grey85")
    if hist["count"] > 0:
        out.append(("   Toplam boşaltılan: " if tr else "   Total freed: "), style="grey70")
        out.append(f"{human_size(hist['total_freed'])}", style="bold bright_green")
        out.append((f"   ({hist['count']} işlem)\n" if tr else f"   ({hist['count']} operations)\n"), style="grey62")
        if hist["last_ts"] and hist_limit > 0:
            out.append((f"   Son işlem: {hist['last_ts']}\n" if tr else f"   Last run: {hist['last_ts']}\n"), style="grey62")
        for e in hist["recent"]:
            name = os.path.basename(e["path"].rstrip("\\/")) or e["path"]
            out.append(f"     {human_size(e['size']):>9}  ", style="cyan")
            out.append(f"{name[:40]}\n", style="grey70")
    else:
        out.append(("   Henüz temizlik kaydı yok\n" if tr else "   No cleanup records yet\n"), style="grey50")

    out.append("\n  ", style="grey50")
    out.append("[C]", style="bright_cyan"); out.append((" hızlı temizlik   " if tr else " quick clean   "), style="grey70")
    out.append("[/]", style="bright_cyan"); out.append((" komutlar   " if tr else " commands   "), style="grey70")
    out.append("[S]", style="bright_cyan"); out.append((" canlı durum   " if tr else " live status   "), style="grey70")
    out.append("[Q]", style="bright_cyan"); out.append((" çıkış" if tr else " quit"), style="grey70")
    return Group(out)


# ---------- Optimize actions (mode: optimize) ----------
@dataclass
class OptAction:
    title: str
    description: str
    cmd: List[str]
    requires_admin: bool = False
    risk: str = "normal"
    key: str = ""


OPTIMIZE_ACTIONS: List[OptAction] = [
    OptAction("Flush DNS cache",          "ipconfig /flushdns",                                ["ipconfig", "/flushdns"], key="flush-dns"),
    OptAction("Reset Winsock",            "netsh winsock reset (admin)",                       ["netsh", "winsock", "reset"], requires_admin=True, risk="high", key="winsock"),
    OptAction("Reset IP stack",           "netsh int ip reset (admin)",                        ["netsh", "int", "ip", "reset"], requires_admin=True, risk="high", key="ip-reset"),
    OptAction("Clear ARP cache",          "netsh interface ip delete arpcache",                ["netsh", "interface", "ip", "delete", "arpcache"], requires_admin=True, key="arp"),
    OptAction("Clear Prefetch",           "delete C:\\Windows\\Prefetch\\*",                   ["cmd", "/c", "del", "/q", "/f", r"C:\Windows\Prefetch\*"], requires_admin=True, risk="high", key="prefetch"),
    OptAction("Clear Event Logs",         "wevtutil cl (Application+System)",                  ["cmd", "/c", "wevtutil el | findstr /v Microsoft-Windows-LiveId & wevtutil cl Application & wevtutil cl System"], requires_admin=True, risk="high", key="event-logs"),
    OptAction("Reset Windows Update",     "stop wuauserv, delete SoftwareDistribution",        ["cmd", "/c", "echo reset windows update"], requires_admin=True, risk="high", key="windows-update"),
    OptAction("Clear Store cache",        "WSReset",                                            ["WSReset.exe"], key="store-cache"),
    OptAction("Storage Sense (open)",     "open Storage settings",                             ["cmd", "/c", "start ms-settings:storagesense"], key="storage-sense"),
    OptAction("Disk Cleanup",             "open cleanmgr",                                     ["cleanmgr"], key="cleanmgr"),
]


def _is_service_running(name: str) -> bool:
    try:
        r = subprocess.run(["sc", "query", name], capture_output=True, text=True, timeout=10, shell=False)
        return "RUNNING" in (r.stdout or "")
    except (subprocess.SubprocessError, OSError):
        return False


def _run_windows_update_reset(dry_run: bool) -> str:
    if dry_run:
        return "dry-run: would stop wuauserv, clear SoftwareDistribution, restore service state"
    was_running = _is_service_running("wuauserv")
    try:
        if was_running:
            subprocess.run(["net", "stop", "wuauserv"], capture_output=True, text=True, timeout=60, shell=False)
        # SystemRoot yerine sabit C:\Windows kullanmak farklı sistem sürücülerinde kırılıyor.
        soft_dist = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "SoftwareDistribution"
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(soft_dist)],
                       capture_output=True, text=True, timeout=120, shell=False)
        if was_running:
            subprocess.run(["net", "start", "wuauserv"], capture_output=True, text=True, timeout=60, shell=False)
        return "ok: Reset Windows Update"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return str(e)


def launch_uninstaller(uninstall_string: str) -> None:
    """Registry'den gelen UninstallString'i cmd shell'e sokmadan başlat.

    shell=True, string içindeki `&`/`|`/`>` karakterlerini komut ayırıcı yapar;
    CreateProcess'e ham komut satırı vermek bu enjeksiyon yüzeyini kapatır.
    %ProgramFiles% gibi değişkenleri shell yerine burada genişletiyoruz.
    """
    subprocess.Popen(os.path.expandvars(uninstall_string), shell=False)


def run_optimize(action: OptAction, dry_run: bool = False) -> tuple:
    """Returns (ok: bool, message: str)."""
    if action.key == "windows-update":
        msg = _run_windows_update_reset(dry_run=dry_run)
        return (msg.startswith("ok") or msg.startswith("dry-run"), msg)
    if dry_run:
        return (True, f"dry-run: would run {action.description}")
    try:
        r = subprocess.run(action.cmd, capture_output=True, text=True, timeout=120, shell=False)
        if r.returncode == 0:
            return (True, f"ok: {action.title}")
        return (False, f"exit {r.returncode}: {r.stderr.strip() or r.stdout.strip()}")
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    except FileNotFoundError as e:
        return (False, f"missing: {e}")
    except Exception as e:
        return (False, str(e))


# ---------- Uninstaller (mode: uninstall) ----------
def list_installed_apps() -> List[dict]:
    """Read Windows installed programs from registry uninstall keys."""
    apps: List[dict] = []
    if os.name != "nt":
        return apps
    import winreg
    roots = [
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKCU", winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive_name, hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as h:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(h, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(h, name) as sk:
                            def g(k):
                                try: return winreg.QueryValueEx(sk, k)[0]
                                except OSError: return ""
                            dn = g("DisplayName")
                            if not dn:
                                continue
                            apps.append({
                                "name": dn,
                                "version": g("DisplayVersion"),
                                "publisher": g("Publisher"),
                                "install_location": g("InstallLocation"),
                                "display_icon": g("DisplayIcon"),
                                "estimated_size_kb": g("EstimatedSize"),
                                "uninstall": g("UninstallString") or g("QuietUninstallString"),
                                "uninstall_key": f"{hive_name}\\{sub}\\{name}",
                            })
                    except OSError:
                        pass
        except OSError:
            pass
    # dedupe by name+version
    seen, out = set(), []
    for a in apps:
        k = (a["name"], a["version"])
        if k not in seen:
            seen.add(k); out.append(a)
    out.sort(key=lambda a: a["name"].lower())
    return out


# ---------- UI ----------
console = Console(soft_wrap=False, highlight=False)

BAR_COLORS = ["bright_yellow", "blue", "blue", "blue", "green3"]
DIM_COLOR = "grey30"
BAR_WIDTH = 38
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def render_bar(pct: float, idx: int, max_pct: float, has_size: bool = True) -> Text:
    color = BAR_COLORS[idx] if idx < len(BAR_COLORS) else DIM_COLOR
    filled = int(round(BAR_WIDTH * pct / max_pct)) if max_pct > 0 else 0
    if has_size and pct > 0 and filled < 1:
        filled = 1
    filled = max(0, min(filled, BAR_WIDTH))
    bar = Text()
    if filled >= 3:
        bar.append("█" * (filled - 2), style=color)
        bar.append("▓▒", style=color)
    elif filled > 0:
        bar.append("█" * filled, style=color)
    bar.append("·" * (BAR_WIDTH - filled), style=DIM_COLOR)
    return bar


def animated_bar(width: int = BAR_WIDTH, color: str = "bright_cyan", phase: float = 0.0) -> Text:
    t = time.time() * 14.0 + phase
    period = (width - 1) * 2
    p = int(t) % period
    if p >= width:
        p = period - p
    blob_w = 6
    start = max(0, p - blob_w // 2)
    end = min(width, start + blob_w)
    bar = Text()
    bar.append("·" * start, style=DIM_COLOR)
    if end - start >= 2:
        bar.append("▓" + "█" * max(0, end - start - 2) + "▒", style=color)
    else:
        bar.append("█" * (end - start), style=color)
    bar.append("·" * (width - end), style=DIM_COLOR)
    return bar


def spinner_char() -> str:
    return SPINNER[int(time.time() * 10) % len(SPINNER)]


def _wrap_tokens(tokens: List[str], width: int, prefix: str = "") -> List[str]:
    lines: List[str] = []
    cur = prefix
    for tok in tokens:
        sep = " | " if cur.strip() else ""
        nxt = cur + sep + tok
        if len(nxt) > width and cur.strip():
            lines.append(cur)
            cur = tok if not prefix else prefix + tok
        else:
            cur = nxt
    if cur.strip():
        lines.append(cur)
    return lines


def build_footer_lines(width: int) -> List[str]:
    main = (
        [
            "↑/↓",
            "Enter",
            "Boşluk Seç",
            "D Sil",
            "K Kalıcı Mod",
            "R Yenile",
            "O Aç",
            "L Artıklar",
            "G Büyük",
            "V Sürücüler",
            "\\ TR/EN",
        ] if LANG == "tr" else [
            "Up/Down",
            "Enter",
            "Space Pick",
            "D Delete",
            "K Permanent",
            "R Refresh",
            "O Open",
            "L Leftovers",
            "G Large",
            "V Drives",
            "\\ TR/EN",
        ]
    )
    modes = [
        "/ İşlemler" if LANG == "tr" else "/ Operations",
        T("help_key"),
        "Esc/Q Geri" if LANG == "tr" else "Esc/Q Back",
    ]
    lines = _wrap_tokens(main, max(40, width))
    lines.extend(_wrap_tokens(modes, max(40, width), prefix="Input: "))
    return lines


@dataclass
class View:
    title: str
    kind: str   # 'cats' | 'items' | 'status' | 'optimize' | 'uninstall' | 'help'
    category: Optional[Category] = None
    scroll: int = 0  # used by 'help'


def _help_sections_tr() -> List[tuple]:
    return [
        ("wmole nedir?", [
            "Windows için derin temizlik, optimize, kaldırma ve geliştirici",
            "artıkları temizleme aracı. Tüm silmeler varsayılan olarak Geri",
            "Dönüşüm Kutusu'na gider; KALICI mod açıkça onay ister.",
        ]),
        ("Komut girdisi ile işlemler", [
            "/analyze          Dosya sistemini gez, klasör boyutlarını gör",
            "/categories       Kategorilere göre toplu disk haritası",
            "/clean            Güvenli temizlik kategorilerini incele",
            "/purge            node_modules, build, venv vb. dev klasörleri",
            "/installers       İndirilenlerdeki eski .exe/.msi setupları",
            "/uninstall        Kurulu programlar + artık temizliği",
            "/optimize         DNS flush, Windows Update reset, Storage Sense",
            "/status           Canlı CPU/RAM/Disk/Ağ/Sıcaklık dashboard",
            "/ports            Dinleyen geliştirici portlarının özetini göster",
            "/update           Güncel sürüm durumunu göster",
            "/help             Bu yardım ekranını aç",
            "/large /drives    Büyük dosyalar / sürücü seçici",
            "/select /delete   Seçimi değiştir / seçilenleri onayla sil",
            "/permanent        Kalıcı silme modunu aç/kapa",
            "/open /refresh    Explorer'da aç / taramayı yenile",
            "/leftovers        Seçili uygulamanın artıklarını tara",
            "/lang /back /quit Dil değiştir / geri dön / çık",
        ]),
        ("Liste navigasyonu", [
            "↑/↓             imleci taşı",
            "Enter           kategori/klasör aç, eylem çalıştır",
            "Space           seç / seçimi kaldır (kategoride: tümü)",
            "D               seçilenleri sil (onay diyaloğu açılır)",
            "K               KALICI mod aç/kapa (kırmızı = geri alınamaz)",
            "O               geçerli yolu Explorer'da aç",
            "R               taramayı yenile",
            "Q / Esc         bir seviye geri / TUI'den çık",
        ]),
        ("Dosya sistemi modunda ekstra", [
            "G               eşik üstü büyük dosyaları listele (config: large_file_min_mb)",
            "V               sürücü seçici (C:, D:, …)",
        ]),
        ("Kaldırma modunda ekstra", [
            "L               seçili uygulamanın artık dosya/registry adaylarını tara",
        ]),
        ("Güvenlik", [
            "• Varsayılan: send2trash → Geri Dönüşüm Kutusu'na taşır.",
            "• K tuşu ile KALICI mod açılır; başlık kırmızıya döner.",
            "• ~/.wmole/whitelist.txt içinde listelenen yollara dokunulmaz.",
            "• ~/.wmole/denylist.txt asla taranmaz (purge bile).",
            "• Korumalı yollar (C:\\Windows, Program Files…) otomatik atlanır.",
            "• Tüm silme/kaldırma/güncelleme ~/.wmole/logs/operations.log'a yazılır.",
        ]),
        ("CLI komutları (terminal)", [
            "wmole                       TUI'yi başlatır (Analyze görünümü)",
            "wmole clean [--dry-run]     güvenli kategorileri tek onayla siler",
            "wmole purge [paths…]        node_modules/build/dist topluca sil",
            "wmole installers            Downloads/Desktop'taki eski setupları bul",
            "wmole uninstall --query x   kurulu programları filtrele",
            "wmole optimize              DNS flush, WU reset, Storage Sense vs.",
            "wmole status [--json]       sistem dashboard / makine-okur çıktı",
            "wmole ports                 dinleyen geliştirici portlarını listele",
            "wmole ports --kill 5173     porta veya PID'e göre süreci öldür",
            "wmole ports --kill all      tüm localhost dinleyicilerini kapat",
            "wmole update                yeniyse GitHub'dan installer ile yükselt",
            "wmole update --disable-auto arka plan otomatik güncellemeyi kapat",
        ]),
        ("Otomatik güncelleme", [
            "Her başlatmada GitHub Releases sessizce kontrol edilir (6 saatte 1).",
            "Yeni sürüm varsa installer %TEMP%'e indirilir ve wmole çıkışında",
            "/VERYSILENT modda kurulur. Bir dahaki açılışta yeni sürümdesiniz.",
            "Devre dışı: wmole update --disable-auto  veya  WMOLE_NO_AUTO_UPDATE=1",
        ]),
        ("İpuçları", [
            "• T tuşu (henüz globalde değil, plan) → --dry-run önizleme",
            "• \\ tuşu Türkçe ↔ English geçişi (config'e yazılır)",
            "• Tarama büyük disklerde 1-2 dk sürebilir, animasyon aktiftir.",
            "• Sorun bildirme: https://github.com/palamut62/wmole/issues",
        ]),
    ]


def _help_sections_en() -> List[tuple]:
    return [
        ("What is wmole?", [
            "A Windows toolkit for deep cleaning, optimization, uninstall, and",
            "developer-leftover sweeps. All deletes go to the Recycle Bin by",
            "default; PERMANENT mode requires explicit opt-in.",
        ]),
        ("Operations via command input", [
            "/analyze          Browse the filesystem with live folder sizes",
            "/categories       Category breakdown of disk usage",
            "/clean            Review safe cleanup categories",
            "/purge            node_modules, build, venv & friends",
            "/installers       Old .exe/.msi setups in Downloads",
            "/uninstall        Installed programs + leftover sweep",
            "/optimize         DNS flush, Windows Update reset, Storage Sense",
            "/status           Live CPU/RAM/Disk/Net/Temperature dashboard",
            "/ports            Show listening developer ports",
            "/update           Show current update status",
            "/help             Open this help screen",
            "/large /drives    Large files / drive picker",
            "/select /delete   Toggle selection / confirm selected delete",
            "/permanent        Toggle permanent-delete mode",
            "/open /refresh    Open in Explorer / refresh current scan",
            "/leftovers        Scan leftovers for selected program",
            "/lang /back /quit Switch language / return / exit",
        ]),
        ("List navigation", [
            "↑/↓             move cursor",
            "Enter           open category/folder, run action",
            "Space           toggle selection (on a category: select all)",
            "D               delete selected (confirm dialog opens)",
            "K               toggle PERMANENT mode (red = non-recoverable)",
            "O               open current path in Explorer",
            "R               refresh scan",
            "Q / Esc         back one level / exit TUI",
        ]),
        ("FS mode extras", [
            "G               list files larger than threshold (config: large_file_min_mb)",
            "V               drive picker (C:, D:, …)",
        ]),
        ("Uninstall extras", [
            "L               scan leftover file + registry candidates for the selected app",
        ]),
        ("Safety", [
            "• Default: send2trash → moves into Recycle Bin.",
            "• K toggles PERMANENT mode; header turns red.",
            "• Paths in ~/.wmole/whitelist.txt are never touched.",
            "• ~/.wmole/denylist.txt is never even scanned (even by purge).",
            "• Protected paths (C:\\Windows, Program Files…) auto-skipped.",
            "• All delete/uninstall/update actions log to ~/.wmole/logs/operations.log.",
        ]),
        ("CLI commands (terminal)", [
            "wmole                       launch TUI (Analyze view)",
            "wmole clean [--dry-run]     one-confirm wipe of safe categories",
            "wmole purge [paths…]        bulk-delete node_modules/build/dist",
            "wmole installers            find old setups in Downloads/Desktop",
            "wmole uninstall --query x   filter installed programs",
            "wmole optimize              DNS flush, WU reset, Storage Sense, …",
            "wmole status [--json]       system dashboard / machine-readable",
            "wmole ports                 list listening developer ports",
            "wmole ports --kill 5173     kill by port or PID",
            "wmole ports --kill all      close every localhost listener",
            "wmole update                check GitHub, upgrade in place if newer",
            "wmole update --disable-auto turn off background auto-update",
        ]),
        ("Auto-update", [
            "Every launch silently checks GitHub Releases (throttled to 6h).",
            "If newer, installer is pre-downloaded to %TEMP% and installed",
            "with /VERYSILENT when wmole exits. Next run = new version.",
            "Opt out: wmole update --disable-auto  or  WMOLE_NO_AUTO_UPDATE=1",
        ]),
        ("Tips", [
            "• \\ toggles Turkish ↔ English (persisted in config).",
            "• Scans can take 1-2 min on large disks; animation indicates progress.",
            "• Report issues: https://github.com/palamut62/wmole/issues",
        ]),
    ]


# ---------- Command palette (Claude Code-style "/") ----------
def palette_commands() -> List[dict]:
    """The catalog shown when the user presses `/` in the TUI."""
    tr = LANG == "tr"
    def d(en, tr_): return tr_ if tr else en
    return [
        {"name": "dashboard", "desc": d("System overview, cleanup analysis & history", "Sistem özeti, temizlik analizi ve geçmiş"), "action": "view:dashboard"},
        {"name": "analyze",   "desc": d("Browse the filesystem with live sizes", "Dosya sistemini canlı boyutlarla gez"),     "action": "view:analyze-fs"},
        {"name": "categories","desc": d("Disk usage by category",                "Kategoriye göre disk dağılımı"),             "action": "view:cats"},
        {"name": "clean",     "desc": d("Review safe cleanup categories",        "Güvenli temizlik kategorilerini incele"),    "action": "view:clean"},
        {"name": "purge",     "desc": d("Bulk-delete node_modules/build/dist",   "node_modules/build/dist toplu sil"),         "action": "view:purge"},
        {"name": "installers","desc": d("Find old setups in Downloads/Desktop",  "İndirilenlerdeki eski setupları bul"),       "action": "view:installers"},
        {"name": "uninstall", "desc": d("Installed programs + leftover sweep",   "Kurulu programlar + artık temizliği"),       "action": "view:uninstall"},
        {"name": "optimize",  "desc": d("DNS flush, Windows Update reset, …",    "DNS flush, Windows Update reset, …"),        "action": "view:optimize"},
        {"name": "status",    "desc": d("Live CPU/RAM/Disk/Net dashboard",       "Canlı CPU/RAM/Disk/Ağ dashboard"),           "action": "view:status"},
        {"name": "ports",     "desc": d("List listening dev ports (run CLI to kill)", "Dinleyen dev portları (öldürme CLI'da)"), "action": "exec:ports"},
        {"name": "update",    "desc": d("Check GitHub update status",            "GitHub güncelleme durumunu kontrol et"),     "action": "exec:update"},
        {"name": "help",      "desc": d("Open the help & features screen",      "Yardım & özellikler ekranını aç"),           "action": "view:help"},
        {"name": "doctor",    "desc": d("Dev machine health report (CLI)",       "Geliştirici makine sağlık raporu (CLI)"),    "action": "exec:doctor"},
        {"name": "undo",      "desc": d("List/restore quarantined deletions (CLI)", "Karantinadaki silmeleri geri al (CLI)"),  "action": "exec:undo"},
        {"name": "hook",      "desc": d("Install the secret-scan pre-commit hook (CLI)", "Secret tarayan pre-commit hook kur (CLI)"), "action": "exec:hook"},
        {"name": "large",     "desc": d("List large files in this folder",       "Bu klasördeki büyük dosyaları listele"),     "action": "key:g"},
        {"name": "drives",    "desc": d("Open the drive picker",                 "Sürücü seçiciyi aç"),                        "action": "key:v"},
        {"name": "select",    "desc": d("Toggle current item selection",         "Geçerli öğe seçimini değiştir"),             "action": "key:SPACE"},
        {"name": "delete",    "desc": d("Delete selected items with confirmation", "Seçilenleri onayla sil"),                   "action": "key:d"},
        {"name": "permanent", "desc": d("Toggle permanent-delete mode",          "Kalıcı silme modunu değiştir"),              "action": "key:k"},
        {"name": "open",      "desc": d("Open current item in Explorer",         "Geçerli öğeyi Explorer'da aç"),              "action": "key:o"},
        {"name": "refresh",   "desc": d("Refresh the current scan",              "Geçerli taramayı yenile"),                   "action": "key:r"},
        {"name": "leftovers", "desc": d("Scan leftovers for selected program",   "Seçili programın artıklarını tara"),         "action": "key:l"},
        {"name": "lang",      "desc": d("Switch Turkish / English",              "Türkçe / İngilizce değiştir"),               "action": "key:\\"},
        {"name": "back",      "desc": d("Return to the previous surface",        "Önceki ekrana dön"),                         "action": "key:ESC"},
        {"name": "quit",      "desc": d("Exit wmole",                            "wmole'dan çık"),                             "action": "exec:quit"},
    ]


def filter_palette(query: str) -> List[dict]:
    q = query.strip().lower().lstrip("/")
    cmds = palette_commands()
    if not q:
        return cmds
    return [c for c in cmds if q in c["name"].lower() or q in c["desc"].lower()]


def render_palette(query: str, cursor: int, max_rows: int = 12) -> Panel:
    rows = filter_palette(query)
    if not rows:
        body = Text("  (no matching command)\n", style="grey50")
    else:
        page_start = max(0, min(cursor - (max_rows - 1), len(rows) - max_rows))
        visible_rows = rows[page_start:page_start + max_rows]
        body = Text()
        for offset, c in enumerate(visible_rows):
            i = page_start + offset
            if i == cursor:
                body.append(
                    f"  > /{c['name']:<12}  {c['desc']}\n",
                    style="bold black on bright_cyan",
                )
            else:
                body.append(f"    /{c['name']:<12}", style="white")
                body.append(f"  {c['desc']}\n", style="grey62")
    header = Text()
    header.append(" / ", style="bold magenta on grey15")
    header.append(query, style="bold white")
    header.append("█", style="bright_magenta")
    header.append("\n", style="white")
    hint = Text("\n  [↑/↓] choose   [Enter] run   [Esc] cancel   [Backspace] edit\n",
                style="grey50")
    return Panel(Group(header, body, hint),
                 title=("Commands" if LANG != "tr" else "Komutlar"),
                 border_style="bright_magenta", padding=(0, 1))


def render_command_input() -> Panel:
    hint = "İşlemleri açmak için / yazın" if LANG == "tr" else "Type / to open operations"
    line = Text()
    line.append(" > ", style="bold bright_magenta")
    line.append(hint, style="grey62")
    return Panel(line, border_style="grey35", padding=(0, 1))


def palette_visible_rows(terminal_height: int) -> int:
    """Use available height for the menu when its frame is open."""
    return max(1, min(12, terminal_height - 20))


def palette_extra_height(query: str, max_rows: int = 12) -> int:
    """Rows consumed beyond the idle command input when results are open."""
    return min(len(filter_palette(query)), max_rows) + 5


def T_update(key: str, **kw) -> str:
    # A custom translation dictionary for the update modal to keep code organized
    strings = {
        "tr": {
            "checking": "Güncelleme kontrol ediliyor, lütfen bekleyin...",
            "git_updating": "Kaynak kodu güncelleniyor (git pull)...",
            "git_success": "Kaynak kodu başarıyla güncellendi!\n{output}",
            "git_fail": "Kaynak kod güncellemesi başarısız oldu:\n{output}",
            "up_to_date": "Uygulama zaten güncel (v{ver}).",
            "fail_reach": "GitHub API'sine ulaşılamadı. Lütfen internet bağlantınızı kontrol edin.",
            "no_asset": "Bu sürüm için uygun bir yükleyici bulunamadı.",
            "ready_title": "  GÜNCELLEME HAZIR  ",
            "ready_prompt": "Yeni sürüm (v{latest}) zaten indirildi ve hazır!\n\nŞimdi uygulayıp wmole'u yeniden başlatmak istiyor musunuz?\n\n[Enter] Evet (Yeniden Başlat)  ·  [Esc] İptal",
            "found_title": "  GÜNCELLEME MEVCUT  ",
            "found_prompt": "Yeni sürüm mevcut: v{latest}\n\nİndirmek istiyor musunuz?\n\n[Enter] İndirmeyi Başlat  ·  [Esc] İptal",
            "downloading": "Yükleyici indiriliyor: {name}\nİlerleme: {pct:>3d}%",
            "download_fail": "İndirme başarısız oldu: {err}",
            "download_success_title": "  İNDİRME TAMAMLANDI  ",
            "download_success_prompt": "Yeni sürüm (v{latest}) başarıyla indirildi!\n\nŞimdi kurmak ve wmole'u yeniden başlatmak istiyor musunuz?\n\n[Enter] Evet (Kur ve Başlat)  ·  [Esc] Hayır (Çıkışta uygulansın)",
            "postpone": "Güncelleme indirildi, wmole kapatıldığında otomatik kurulacaktır.",
            "applying": "Güncelleme kuruluyor, wmole kapatılıyor...",
        },
        "en": {
            "checking": "Checking for updates, please wait...",
            "git_updating": "Updating source code (git pull)...",
            "git_success": "Source code updated successfully!\n{output}",
            "git_fail": "Source code update failed:\n{output}",
            "up_to_date": "Application is already up to date (v{ver}).",
            "fail_reach": "Could not reach GitHub API. Please check your connection.",
            "no_asset": "No installer asset found for this release.",
            "ready_title": "  UPDATE READY  ",
            "ready_prompt": "New version (v{latest}) is already downloaded and ready!\n\nDo you want to apply it and restart wmole now?\n\n[Enter] Yes (Restart)  ·  [Esc] Cancel",
            "found_title": "  UPDATE AVAILABLE  ",
            "found_prompt": "New version available: v{latest}\n\nDo you want to download it?\n\n[Enter] Start Download  ·  [Esc] Cancel",
            "downloading": "Downloading installer: {name}\nProgress: {pct:>3d}%",
            "download_fail": "Download failed: {err}",
            "download_success_title": "  DOWNLOAD COMPLETE  ",
            "download_success_prompt": "New version (v{latest}) successfully downloaded!\n\nDo you want to apply it and restart wmole now?\n\n[Enter] Yes (Install & Restart)  ·  [Esc] No (Apply on exit)",
            "postpone": "Update staged, will be installed automatically when you close wmole.",
            "applying": "Applying update, closing wmole...",
        }
    }
    s = strings.get(LANG, strings["tr"]).get(key) or strings["en"].get(key, key)
    return s.format(**kw) if kw else s


def interactive_tui_update(live: Live, scanner: Scanner, view: View, cursor: int,
                            apps_cache: List[dict], opt_cache: List[dict],
                            use_trash: bool, dry_run: bool) -> str:
    border_color = "red" if not use_trash else "magenta"
    
    # 1. Developer / Source code Mode (sys.frozen is False)
    if not getattr(sys, "frozen", False):
        body_txt = Text("\n" + T_update("git_updating") + "\n", style="bold cyan")
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title="wmole update",
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        
        # Perform git pull and dependency upgrade
        steps = []
        if path_exists(Path(".git")):
            r = subprocess.run(["git", "pull", "--ff-only"], capture_output=True, text=True, shell=False)
            git_ok = (r.returncode == 0)
            git_out = (r.stdout or r.stderr).strip()
        else:
            git_ok = False
            git_out = "not a git repository"
            
        if git_ok:
            # Upgrade dependencies
            r2 = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "rich", "send2trash", "psutil"],
                                capture_output=True, text=True, shell=False)
            body_txt = Text("\n" + T_update("git_success", output=git_out) + "\n", style="bold green")
        else:
            body_txt = Text("\n" + T_update("git_fail", output=git_out) + "\n", style="bold red")
            
        body_txt.append("\n\n" + T("confirm_keys").split("·")[-1].strip(), style="bold cyan") # Esc to close
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title="wmole update",
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        while True:
            k = read_key()
            if k in ("ESC", "ENTER"):
                break
        return T_update("git_success", output="") if git_ok else T_update("git_fail", output="")

    # 2. Frozen/Exe Mode
    # First show "Checking..."
    body_txt = Text("\n" + T_update("checking") + "\n", style="bold cyan")
    modal = Panel(
        Align.center(body_txt, vertical="middle"),
        title="wmole update",
        title_align="center",
        border_style=border_color,
        padding=(1, 2)
    )
    live.update(modal)
    
    rel = fetch_latest_release()
    if not rel:
        body_txt = Text("\n" + T_update("fail_reach") + "\n", style="bold red")
        body_txt.append("\n\n" + T("confirm_keys").split("·")[-1].strip(), style="bold cyan")
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title="wmole update",
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        while True:
            k = read_key()
            if k in ("ESC", "ENTER"):
                break
        return T_update("fail_reach")
        
    latest_tag = rel.get("tag_name", "")
    latest_ver = _parse_semver(latest_tag)
    current_ver = _parse_semver(__version__)
    
    if latest_ver <= current_ver:
        body_txt = Text("\n" + T_update("up_to_date", ver=__version__) + "\n", style="bold green")
        body_txt.append("\n\n" + T("confirm_keys").split("·")[-1].strip(), style="bold cyan")
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title="wmole update",
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        while True:
            k = read_key()
            if k in ("ESC", "ENTER"):
                break
        return T_update("up_to_date", ver=__version__)
        
    asset = _pick_installer_asset(rel.get("assets", []))
    if not asset:
        body_txt = Text("\n" + T_update("no_asset") + "\n", style="bold red")
        body_txt.append("\n\n" + T("confirm_keys").split("·")[-1].strip(), style="bold cyan")
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title="wmole update",
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        while True:
            k = read_key()
            if k in ("ESC", "ENTER"):
                break
        return T_update("no_asset")
        
    url = asset["browser_download_url"]
    name = Path(asset["name"]).name  # strip any path components from the API value
    expected_size = int(asset.get("size") or 0)
    dest = TEMP / name
    
    # Check if we already have this exact installer staged.
    already_staged = False
    if PENDING_UPDATE_FILE.exists():
        try:
            cur = json.loads(PENDING_UPDATE_FILE.read_text(encoding="utf-8"))
            cur_path = Path(cur.get("path", ""))
            if (cur.get("version") == latest_tag and cur_path.exists()
                    and (expected_size == 0 or cur_path.stat().st_size == expected_size)):
                already_staged = True
        except Exception:
            pass
            
    if already_staged:
        body_txt = Text("\n" + T_update("ready_prompt", latest=latest_tag) + "\n", style="bold green")
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title=T_update("ready_title"),
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        
        while True:
            k = read_key()
            if k == "ENTER":
                # Apply immediately
                body_txt = Text("\n" + T_update("applying") + "\n", style="bold green")
                modal = Panel(Align.center(body_txt, vertical="middle"), title="wmole update", border_style=border_color)
                live.update(modal)
                time.sleep(0.8)
                # Smart launch: uses setup flags for installers, batch-swap for raw EXEs
                _launch_updated(dest, name, latest_tag)
                # _launch_updated calls os._exit(0), so we never reach here
            elif k == "ESC":
                return f"update available: {latest_tag}"
                
    # If not staged, prompt to download
    body_txt = Text("\n" + T_update("found_prompt", latest=latest_tag) + "\n", style="bold yellow")
    modal = Panel(
        Align.center(body_txt, vertical="middle"),
        title=T_update("found_title"),
        title_align="center",
        border_style=border_color,
        padding=(1, 2)
    )
    live.update(modal)
    
    k = read_key()
    if k != "ENTER":
        return T("cancelled")
        
    # Start download with live progress bar
    try:
        def _prog(read, total):
            pct = int(read * 100 / total)
            # Create a simple ASCII/Unicode progress bar
            bar_len = 30
            filled = int(bar_len * read / total)
            bar_str = "█" * filled + "░" * (bar_len - filled)
            
            body_txt = Text("\n" + T_update("downloading", name=name, pct=pct) + "\n\n", style="bold cyan")
            body_txt.append(f"  {bar_str}  {pct:3d}%\n", style="bright_blue")
            modal = Panel(
                Align.center(body_txt, vertical="middle"),
                title="wmole update",
                title_align="center",
                border_style=border_color,
                padding=(1, 2)
            )
            live.update(modal)
            
        _download(url, dest, on_progress=_prog)

        # Verify download integrity against expected size
        if expected_size > 0 and dest.stat().st_size != expected_size:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            raise IOError("size mismatch")

        # Successfully downloaded, write pending file
        PENDING_UPDATE_FILE.write_text(json.dumps({
            "version": latest_tag,
            "name": name,
            "path": str(dest),
            "downloaded_at": time.time(),
            "from_version": __version__,
        }), encoding="utf-8")
        
        # Save check file so background doesn't hit GitHub again soon
        try:
            WMOLE_DIR.mkdir(parents=True, exist_ok=True)
            UPDATE_CHECK_FILE.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
        except Exception:
            pass
            
    except Exception as exc:
        body_txt = Text("\n" + T_update("download_fail", err=str(exc)) + "\n", style="bold red")
        body_txt.append("\n\n" + T("confirm_keys").split("·")[-1].strip(), style="bold cyan")
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title="wmole update",
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        while True:
            k = read_key()
            if k in ("ESC", "ENTER"):
                break
        return T_update("download_fail", err=str(exc))
        
    # Download successful! Prompt for restart
    body_txt = Text("\n" + T_update("download_success_prompt", latest=latest_tag) + "\n", style="bold green")
    modal = Panel(
        Align.center(body_txt, vertical="middle"),
        title=T_update("download_success_title"),
        title_align="center",
        border_style=border_color,
        padding=(1, 2)
    )
    live.update(modal)
    
    k = read_key()
    if k == "ENTER":
        # Apply immediately
        body_txt = Text("\n" + T_update("applying") + "\n", style="bold green")
        modal = Panel(Align.center(body_txt, vertical="middle"), title="wmole update", border_style=border_color)
        live.update(modal)
        time.sleep(0.8)
        # Smart launch: uses setup flags for installers, batch-swap for raw EXEs
        _launch_updated(dest, name, latest_tag)
        # _launch_updated calls os._exit(0), so we never reach here
    else:
        # User postponed
        body_txt = Text("\n" + T_update("postpone") + "\n", style="bold green")
        body_txt.append("\n\n" + T("confirm_keys").split("·")[-1].strip(), style="bold cyan")
        modal = Panel(
            Align.center(body_txt, vertical="middle"),
            title="wmole update",
            title_align="center",
            border_style=border_color,
            padding=(1, 2)
        )
        live.update(modal)
        while True:
            k = read_key()
            if k in ("ESC", "ENTER"):
                break
        return f"update available: {latest_tag}"


def format_tui_update_status(status: str) -> str:
    if LANG != "tr":
        return status
    if status == "already up to date":
        return "Sürüm güncel."
    if status.startswith("update available: "):
        latest = status.split(";", 1)[0].removeprefix("update available: ")
        return f"Güncelleme mevcut: {latest}. Otomatik kurulum için wmole'u yeniden başlatın."
    if status == "could not reach GitHub API":
        return "GitHub güncelleme kontrolüne ulaşılamadı."
    return status


def render_help(scroll: int = 0) -> Group:
    sections = _help_sections_tr() if LANG == "tr" else _help_sections_en()
    body = Text()
    body.append(f"wmole v{__version__}", style="bold bright_cyan")
    body.append("   ", style="grey50")
    body.append("https://wmole.vercel.app", style="grey50")
    body.append("\n\n")
    for title, lines in sections:
        body.append(f"  {title}\n", style="bold bright_yellow")
        for ln in lines:
            body.append(f"    {ln}\n", style="white")
        body.append("\n")
    return Group(body)


def view_rows(view: View, scanner: Scanner) -> List[object]:
    if view.kind == "cats":
        with scanner._lock:
            return [c for c in scanner.categories if c.items or c.scanning]
    if view.kind == "items":
        return [i for i in (view.category.items if view.category else []) if not i.deleted]
    return []


def render(scanner: Scanner, view: View, cursor: int, msg: str,
           use_trash: bool, dry_run: bool, apps_cache: List[dict],
           opt_cache: List[dict],
           palette: Optional[tuple] = None) -> Group:
    total_free = free_space_gb()
    footer_lines = build_footer_lines(console.size.width or 120)

    # Top banner — title reflects the current view
    title = {
        "dashboard": "wmole · Gösterge Paneli" if LANG == "tr" else "wmole · Dashboard",
        "cats":      T("title_analyze"),
        "items":     T("title_inside", name=view.category.title) if view.category else T("title_analyze"),
        "status":    T("title_status"),
        "optimize":  T("title_optimize"),
        "uninstall": T("title_uninstall"),
        "help":      T("title_help"),
    }.get(view.kind, "wmole")
    permanent_mode = not use_trash
    header = Text()
    header.append(title, style="bold red" if permanent_mode else "bold magenta")
    header.append(f"  v{__version__}", style="grey50")
    header.append("  " + T("free_suffix", free=total_free), style="grey62")
    mode_tags = [T("mode_permanent") if permanent_mode else T("mode_trash")]
    if dry_run: mode_tags.append(T("mode_dry_run"))
    tag_style = "bold red on grey15" if permanent_mode else ("bold yellow" if dry_run else "grey70")
    header.append("   " + "  ".join(mode_tags), style=tag_style)
    header.append("\n")
    if view.kind == "dashboard":
        header.append(("Sistem durumu, temizlik analizi ve geçmiş — [C] hızlı temizlik · [/] komutlar" if LANG == "tr"
                       else "System status, cleanup analysis & history — [C] quick clean · [/] commands"), style="grey70")
    elif view.kind == "cats":
        header.append(T("hint_cats"), style="grey70")
    elif view.kind == "items":
        if view.category and view.category.key.startswith("fs:"):
            header.append(T("hint_fs"), style="grey70")
        else:
            header.append(T("hint_items", name=view.category.title if view.category else ""), style="grey70")
    elif view.kind == "status":
        header.append(T("hint_status"), style="grey70")
    elif view.kind == "optimize":
        header.append(T("hint_optimize"), style="grey70")
    elif view.kind == "uninstall":
        header.append(T("hint_uninstall"), style="grey70")
    elif view.kind == "help":
        header.append(T("hint_help"), style="grey70")

    # Body
    palette_limit = (palette_visible_rows(console.size.height or 30) if palette is not None else 12)
    if view.kind == "dashboard":
        # Reserve rows for header (2), footer, the status line, and the 3-row
        # command-input panel so the dashboard body never crops the input box.
        term_h = console.size.height or 30
        body_budget = max(8, term_h - (2 + len(footer_lines) + 1 + 3))
        body = render_dashboard(scanner, body_budget)
        rows_for_pad = 0
        rows = []
    elif view.kind == "help":
        body = render_help(view.scroll)
        rows_for_pad = 0
        rows = []
    elif view.kind == "status":
        # Status mode: fixed-height dashboard
        body = render_status()
        rows_for_pad = 0
        rows = []  # for footer-only path below
    else:
        rows = (view_rows(view, scanner) if view.kind in ("cats", "items")
                else opt_cache if view.kind == "optimize"
                else apps_cache)

        def cat_size(c): return sum((i.partial or i.size) for i in c.items if not i.deleted) if c.scanning else c.total
        def item_size(it): return it.partial if it.scanning and it.partial else it.size

        if view.kind == "cats":
            sizes = [cat_size(r) for r in rows]
        elif view.kind == "items":
            sizes = [item_size(r) for r in rows]
        else:
            sizes = [0] * len(rows)
        total = sum(sizes) or 1
        max_pct = max((s / total * 100 for s in sizes), default=1)

        palette_limit = palette_visible_rows(console.size.height or 30) if palette is not None else 12
        palette_rows = palette_extra_height(palette[0], palette_limit) if palette is not None else 0
        reserved = 12 + len(footer_lines) + palette_rows
        min_body_rows = 1 if palette is not None else 5
        avail = max(min_body_rows, (console.size.height or 30) - reserved)
        if len(rows) > avail:
            start = max(0, min(cursor - avail // 2, len(rows) - avail))
            end = start + avail
        else:
            start, end = 0, len(rows)
        rows_shown = end - start

        body = Text("\n")
        body.append(("      " + T("footer_more", n=start) + "\n" if start > 0 else "\n"), style="grey42")

        for i, row in enumerate(rows):
            if i < start or i >= end:
                continue
            is_cursor = i == cursor
            prefix = Text("  ▶ " if is_cursor else "    ", style="cyan")
            idx_txt = Text(f"{i+1:>2}. ", style="grey62")

            if view.kind == "cats":
                cat: Category = row  # type: ignore[assignment]
                size = cat_size(cat)
                pct = size / total * 100
                is_active = (scanner.current_cat_key == cat.key)
                is_pending = cat.scanning and not is_active and size == 0
                animating = cat.scanning and size == 0
                bar = animated_bar(color="bright_cyan" if is_active else "grey50", phase=i * 3.0) if animating else render_bar(pct, i, max_pct, has_size=size > 0)
                all_sel = bool(cat.items) and all(it.selected or it.deleted for it in cat.items)
                any_sel = any(it.selected for it in cat.items if not it.deleted)
                mark = "✓" if all_sel else ("~" if any_sel else " ")
                mark_style = "bright_green" if all_sel else ("yellow" if any_sel else "grey42")
                spin = (spinner_char() + " ") if cat.scanning else ("  " if is_pending else "")
                name_style = ("bright_cyan" if is_cursor else "bright_yellow" if cat.scanning
                              else "grey42" if is_pending else "white")
                name = Text(f"[{mark}] {spin}{cat.icon} {cat.title}", style=name_style)
                pct_txt = Text(f"  {pct:5.1f}%", style="bright_cyan")
                size_txt = Text(f"{human_size(size):>10}", style="bright_blue" if i < 5 else "grey62")
            elif view.kind == "items":
                it: Item = row  # type: ignore[assignment]
                size = item_size(it)
                pct = size / total * 100
                is_active = (scanner.current_item_id == id(it))
                bar = animated_bar(phase=i * 3.0) if (it.scanning and size == 0) else render_bar(pct, i, max_pct, has_size=size > 0)
                mark = "✓" if it.selected else " "
                mark_style = "bright_green" if it.selected else "grey42"
                disp = str(it.path)
                if len(disp) > 48: disp = "…" + disp[-47:]
                spin = (spinner_char() + " ") if it.scanning else ""
                file_icon = (view.category.icon if view.category else "📁")
                name_style = ("bright_cyan" if is_cursor else "bright_yellow" if it.scanning else "white")
                name = Text(f"[{mark}] {spin}{file_icon} {disp}", style=name_style)
                pct_txt = Text(f"  {pct:5.1f}%", style="bright_cyan")
                size_txt = Text(f"{human_size(size):>10}", style="bright_blue" if i < 5 else "grey62")
            elif view.kind in ("optimize", "uninstall"):
                # Clean two-column, cell-aware, no-wrap layout.
                from rich.cells import cell_len, set_cell_size
                term_w = console.size.width or 120
                right_w = 26
                left_w = max(20, term_w - 4 - 4 - right_w - 2)
                if view.kind == "optimize":
                    act: OptAction = row  # type: ignore[assignment]
                    badge = (" [admin]" if act.requires_admin else "") + (" [high-risk]" if act.risk == "high" else "")
                    left_txt = f"🛠 {act.title}{badge}"
                    right_txt = act.description
                else:
                    app: dict = row  # type: ignore[assignment]
                    left_txt = f"📦 {app['name']}"
                    pub = app.get("publisher") or ""
                    ver = app.get("version") or ""
                    right_txt = (ver + ("  · " + pub if pub else "")).strip() or "—"
                left_padded = set_cell_size(left_txt, left_w)
                right_padded = set_cell_size(right_txt, right_w)
                # right-justify by padding-left to fill right_w
                if cell_len(right_txt) < right_w:
                    right_padded = " " * (right_w - cell_len(right_txt)) + right_txt
                else:
                    right_padded = set_cell_size(right_txt, right_w)
                name_style = "bright_cyan" if is_cursor else "white"
                row_text = Text(no_wrap=True, overflow="crop")
                row_text.append(left_padded, style=name_style)
                row_text.append("  ")
                row_text.append(right_padded, style="grey50")
                line = Text(no_wrap=True, overflow="crop")
                line.append_text(prefix); line.append_text(idx_txt); line.append_text(row_text)
                line.append("\n")
                body.append_text(line)
                continue

            sep = Text(" | ", style="grey42")
            target_w = 50
            cur_w = console.measure(name).maximum
            if cur_w < target_w:
                name.append(" " * (target_w - cur_w))
            line = Text.assemble(prefix, idx_txt, bar, pct_txt, sep, name, " ", size_txt, "\n")
            line.no_wrap = True
            body.append_text(line)

        for _ in range(avail - rows_shown):
            body.append("\n")
        body.append(("      " + T("footer_less", n=len(rows) - end) + "\n" if end < len(rows) else "\n"), style="grey42")

    # Status / message lines (fixed 2) with animation-style message display
    status = Text()
    line1 = ("  " + scanner.status) if (scanner.status and not scanner.done and view.kind in ("cats", "items")) else ""
    line2 = ("  " + msg) if msg else ""
    # On the full-screen info views (dashboard/status/help) the two status rows
    # are always blank and only steal vertical space the input box needs, so
    # collapse them to a single separator row unless there's a message to show.
    if view.kind in ("dashboard", "status", "help") and not line1 and not msg:
        status.append(" ", style="grey50")
        footer = Text("\n".join(footer_lines), style="bold grey70")
        if palette is not None:
            p_query, p_cursor = palette
            return Group(header, body, status, footer, render_palette(p_query, p_cursor, palette_limit))
        return Group(header, body, status, footer, render_command_input())
    status.append((line1 or " ") + "\n", style="grey50")
    # Enhanced message display with icon prefix detection for styled output
    if msg:
        msg_prefix = msg[:3] if len(msg) >= 3 else ""
        msg_style = "bold bright_green" if ("✓" in msg or "✅" in msg or "ok" in msg.lower()[:10] or "Done" in msg[:10] or "Deleted" in msg[:10] or "Cleaned" in msg[:10] or "Removed" in msg[:10]) else \
                    "bold bright_red" if ("✗" in msg or "❌" in msg or "fail" in msg.lower()[:10] or "Error" in msg[:10]) else \
                    "bold bright_yellow" if ("⚠" in msg or "⚠️" in msg or "Warning" in msg[:10] or "warn" in msg.lower()[:10]) else \
                    "bold cyan" if ("cancelled" in msg.lower() or "aborted" in msg.lower()) else \
                    "bold yellow"
        status.append("  " + line2 + "\n", style=msg_style)
    else:
        status.append((line2 or " ") + "\n", style="yellow")

    footer = Text("\n".join(footer_lines), style="bold grey70")
    if palette is not None:
        p_query, p_cursor = palette
        return Group(header, body, status, footer, render_palette(p_query, p_cursor, palette_limit))
    return Group(header, body, status, footer, render_command_input())


# ---------- Actions ----------
def open_in_explorer(p: Path) -> None:
    try:
        os.startfile(str(p))  # type: ignore[attr-defined]
    except OSError:
        subprocess.Popen(["explorer", str(p)])


def confirm_delete(live: Live, scanner: Scanner, view: View, cursor: int,
                   use_trash: bool, dry_run: bool, apps: List[dict],
                   opt: List[dict]) -> str:
    # Collect selected items, partitioned into protected (blocked) and deletable.
    candidates: List[Item] = []
    kinds: Dict[str, str] = {}  # path -> category key, for risk scoring
    if view.kind == "items" and view.category is not None:
        for it in view.category.items:
            if it.selected and not it.deleted:
                candidates.append(it)
                kinds[str(it.path)] = view.category.key
    else:
        for cat in scanner.categories:
            for it in cat.items:
                if it.selected and not it.deleted:
                    candidates.append(it)
                    kinds[str(it.path)] = cat.key
    protected = [it for it in candidates if is_protected_path(it.path)]
    targets   = [it for it in candidates if not is_protected_path(it.path)]
    if not targets:
        return T("nothing_selected") if not protected else T("protected_header", n=len(protected))

    total = sum(t.size for t in targets)
    # Measure what the delete really frees. This is the moment to pay for the
    # extra syscalls: the list is now small and the number is about to be shown
    # as a promise. Budgeted so a pathological tree cannot stall the dialog.
    facts = measure_selection(targets, max_seconds_each=2.0)
    # Scored without the git probe: it shells out per path and the guard in
    # delete_path runs it again for real before anything is removed.
    scored = [(risk_score(t.path, kind=kinds.get(str(t.path), ""), check_git=False), t)
              for t in targets[:50]]
    riskiest = max(scored, key=lambda pair: pair[0].score, default=None)
    if dry_run:    action_label = T("act_dry_run")
    elif use_trash: action_label = T("act_trash")
    else:           action_label = T("act_permanent")

    # Build a scrollable preview: first 10 paths, then "and N more".
    danger = (not use_trash) and (not dry_run)
    border_color = "red" if danger else ("yellow" if dry_run else "cyan")
    body_txt = Text()
    body_txt.append(T("confirm_header", n=len(targets), action=action_label, size=human_size(total)),
                    style="bold red" if danger else "bold yellow")
    body_txt.append("\n")
    # The headline number above is logical size, kept for continuity. What the
    # drive will actually show afterwards is the on-disk figure.
    body_txt.append("  " + T("on_disk_note", size=human_size(facts.on_disk),
                             logical=human_size(facts.logical)) + "\n",
                    style="bright_cyan")
    if facts.truncated:
        body_txt.append("  " + T("on_disk_partial") + "\n", style="grey50")
    if facts.placeholder_files:
        body_txt.append("  ☁ " + T("placeholder_note", n=facts.placeholder_files,
                                   size=human_size(facts.placeholder_bytes)) + "\n",
                        style="yellow")
    if riskiest is not None and riskiest[0].score >= 45:
        risk_obj, risk_item = riskiest
        body_txt.append("  ⚠ " + T("risk_note", score=risk_obj.score,
                                   level=risk_obj.level, path=risk_item.path.name) + "\n",
                        style="red" if risk_obj.score >= 70 else "yellow")
    body_txt.append("\n")
    preview_n = 10
    for it in targets[:preview_n]:
        disp = str(it.path)
        if len(disp) > 70:
            disp = "…" + disp[-69:]
        body_txt.append(f"  • {disp}", style="white")
        body_txt.append(f"   {human_size(it.size)}\n", style="grey50")
    if len(targets) > preview_n:
        body_txt.append(T("and_more", n=len(targets) - preview_n) + "\n", style="grey50")
    if protected:
        body_txt.append("\n" + T("protected_header", n=len(protected)) + "\n", style="bold grey62")
        for it in protected[:5]:
            disp = str(it.path)
            if len(disp) > 70:
                disp = "…" + disp[-69:]
            body_txt.append(f"  ⛔ {disp}\n", style="grey42")
        if len(protected) > 5:
            body_txt.append(T("and_more", n=len(protected) - 5) + "\n", style="grey42")
    body_txt.append("\n" + T("confirm_keys"), style="bold red" if danger else "bold cyan")

    modal = Panel(
        Align.center(body_txt, vertical="top"),
        title=T("confirm_title"),
        title_align="center",
        border_style=border_color,
        padding=(1, 2),
    )
    live.update(modal)
    key = read_key()
    if key != "ENTER":
        return T("cancelled")

    total_count = len(targets)
    ok = err = 0
    start_time = time.time()

    # Render the heavy TUI background ONCE — re-rendering it per item is what
    # made deletion crawl (each render samples psutil cpu/health with blocking
    # intervals). During deletion we only refresh the lightweight progress panel.
    bg = render(scanner, view, cursor, "", use_trash, dry_run, apps, opt)
    spin_frames = ["◜", "◝", "◞", "◟"]

    def draw_progress(done: int, last_path: str, force: bool = False) -> None:
        pct_done = (done / total_count) * 100 if total_count > 0 else 100
        bar_w = 30
        filled = int(bar_w * done / total_count) if total_count > 0 else bar_w
        progress_bar = Text()
        progress_bar.append("[" + "█" * filled + "·" * (bar_w - filled) + "]", style="bright_cyan")
        progress_bar.append(f" {done}/{total_count}", style="white")
        progress_bar.append(f"  {pct_done:5.1f}%", style="bright_cyan")
        elapsed = time.time() - start_time
        elapsed_str = f"{elapsed:.1f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
        disp = last_path
        if len(disp) > 65:
            disp = "…" + disp[-64:]
        current_item = Text()
        current_item.append(f"\n  {spin_frames[done % 4]} Deleting: ", style="yellow")
        current_item.append(disp, style="bold white")
        stats_line = Text()
        stats_line.append(f"\n  ✓ {ok} done", style="bright_green")
        if err > 0:
            stats_line.append(f"  ✗ {err} errors", style="red")
        stats_line.append(f"  ⏱ {elapsed_str}", style="grey62")
        live.update(Group(bg, Panel(
            Group(progress_bar, current_item, stats_line),
            title=T("confirm_title"), title_align="center",
            border_style="cyan", padding=(1, 2),
        )))

    last_ui = 0.0
    if use_trash and not dry_run:
        # Fast path: batch paths to the Recycle Bin in chunks (one COM op each).
        CHUNK = 20
        done = 0
        for start in range(0, total_count, CHUNK):
            chunk = targets[start:start + CHUNK]
            res = trash_paths_batch([it.path for it in chunk],
                                    sizes={str(it.path): it.size for it in chunk})
            for it in chunk:
                e = res.get(str(it.path))
                if e is None:
                    it.deleted = True
                    it.selected = False
                    ok += 1
                else:
                    it.error = e
                    err += 1
                done += 1
            draw_progress(done, str(chunk[-1].path), force=True)
    else:
        # Permanent / dry-run path stays per-item, but UI updates are throttled.
        for idx, it in enumerate(targets):
            now = time.time()
            if now - last_ui > 0.1 or idx == 0:
                draw_progress(idx, str(it.path))
                last_ui = now
            e = delete_path(it.path, use_trash=use_trash, dry_run=dry_run, known_size=it.size)
            if e is None:
                if not dry_run:
                    it.deleted = True
                    it.selected = False
                ok += 1
            else:
                it.error = e
                err += 1

    elapsed_total = time.time() - start_time
    elapsed_total_str = f"{elapsed_total:.1f}s" if elapsed_total < 60 else f"{elapsed_total/60:.1f}m"

    # ── Completion animation ──
    for frame in range(8):
        bar_w = 30
        all_filled = bar_w
        comp_bar = Text()
        comp_bar.append("[" + "█" * all_filled + "]", style="bright_green")
        comp_bar.append(f" {total_count}/{total_count}", style="white")
        comp_bar.append("  100.0%", style="bright_green")

        result_icon = "✅" if err == 0 else "⚠️"
        result_line = Text()
        result_line.append(f"\n  {result_icon} ", style="bright_green" if err == 0 else "bright_yellow")
        if dry_run:
            result_line.append(T("done_dry", n=ok, size=human_size(total)), style="bold white")
        elif use_trash:
            result_line.append(T("done_ok", n=ok, size=human_size(total)), style="bold white")
        else:
            result_line.append(T("done_perm", n=ok, size=human_size(total)), style="bold white")
        if err:
            result_line.append("  " + T("done_errs", n=err), style="red")
        result_line.append(f"  ⏱ {elapsed_total_str}", style="grey62")

        # Pulsing effect on completion
        border_styles = ["bright_green", "green", "bright_green", "green", "bright_green", "green", "cyan", "bright_green"]
        live.update(Group(
            bg,
            Panel(
                Group(comp_bar, result_line),
                title=T("confirm_title") + " — " + ("✓" if err == 0 else "!"),
                title_align="center",
                border_style=border_styles[frame % len(border_styles)],
                padding=(1, 2),
            )
        ))
        time.sleep(0.06)

    if dry_run:
        msg = T("done_dry", n=ok, size=human_size(total))
    elif use_trash:
        msg = T("done_ok", n=ok, size=human_size(total))
    else:
        msg = T("done_perm", n=ok, size=human_size(total))
    if err:
        msg += T("done_errs", n=err)
    return msg


def _save_config_key(key: str, value) -> None:
    """Persist a single key into the user config without disturbing the rest."""
    try:
        cfg = load_config()
        cfg[key] = value
        WMOLE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def run_first_launch_quick_clean() -> None:
    """First-launch flow: quick-scan safe cache/temp categories, show what was
    detected, and—only after the user confirms—send it to the Recycle Bin.

    Runs once; a config flag prevents it from prompting on every start.
    """
    cfg = load_config()
    if cfg.get("quick_clean_v2_done"):
        return

    tr = LANG == "tr"
    title = "wmole · İlk Açılış Temizliği" if tr else "wmole · First-Launch Cleanup"
    whitelist = load_whitelist()
    scanner = Scanner(whitelist=whitelist, profile="clean", use_cache=True)
    threading.Thread(target=scanner.run, daemon=True).start()

    spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    fi = 0
    with Live(console=console, refresh_per_second=12, screen=True) as live:
        # ── Quick scan phase ──
        while not scanner.done:
            body = Text()
            body.append("\n  ")
            body.append(spin[fi % len(spin)], style="bright_cyan")
            body.append(("  Hızlı tarama yapılıyor…  " if tr else "  Quick scan in progress…  "), style="bold white")
            body.append((scanner.status or "")[:48], style="grey62")
            partial = sum(c.total for c in scanner.categories)
            body.append((f"\n\n  Şimdiye dek bulunan: {human_size(partial)}\n" if tr
                         else f"\n\n  Found so far: {human_size(partial)}\n"), style="green")
            live.update(Panel(body, title=title, border_style="cyan", padding=(1, 2)))
            fi += 1
            time.sleep(0.08)

        # ── Build summary of safe, deletable targets ──
        report = collect_selected_targets(scanner.categories, estimated=False)
        targets = [it for c in scanner.categories for it in c.items
                   if it.selected and not it.deleted and not is_protected_path(it.path)]
        total = report["total"]

        if not targets or total <= 0:
            _save_config_key("quick_clean_v2_done", True)
            done = Text(("\n  ✓ Temizlenecek fazla veri bulunamadı. Sistem temiz.\n" if tr
                         else "\n  ✓ Nothing significant to clean. Your system is tidy.\n"), style="bold green")
            live.update(Panel(done, title=title, border_style="green", padding=(1, 2)))
            time.sleep(1.4)
            return

        cat_totals = []
        for c in scanner.categories:
            ct = sum(it.size for it in c.items
                     if it.selected and not it.deleted and not is_protected_path(it.path))
            if ct > 0:
                cat_totals.append((c.title, ct))
        cat_totals.sort(key=lambda x: x[1], reverse=True)

        body = Text()
        body.append(("\n  Tespit edilen fazla veri: " if tr else "\n  Detected clutter: "), style="white")
        body.append(human_size(total), style="bold bright_green")
        body.append((f"   ({len(targets)} öğe)\n\n" if tr else f"   ({len(targets)} items)\n\n"), style="grey62")
        for ttl, ct in cat_totals[:8]:
            body.append(f"   • {ttl[:28]:<28}", style="grey85")
            body.append(f"{human_size(ct):>10}\n", style="cyan")
        if len(cat_totals) > 8:
            body.append((f"   … +{len(cat_totals) - 8} kategori daha\n" if tr
                         else f"   … +{len(cat_totals) - 8} more categories\n"), style="grey50")
        body.append(("\n  Seçilen öğeler Geri Dönüşüm Kutusu'na taşınacak (geri alınabilir).\n" if tr
                     else "\n  Selected items go to the Recycle Bin (recoverable).\n"), style="grey62")
        body.append("\n  [", style="grey50"); body.append("E", style="bold bright_green")
        body.append(("] Temizle     " if tr else "] Clean     "), style="white")
        body.append("[", style="grey50"); body.append("H", style="bold red")
        body.append(("] Atla" if tr else "] Skip"), style="white")
        live.update(Panel(body, title=title, border_style="cyan", padding=(1, 2)))

        # ── Wait for an in-app confirmation key (no native dialog) ──
        choice = None
        while choice is None:
            if os.name == "nt":
                if msvcrt.kbhit():
                    k = read_key().lower()
                    if k in ("e", "y"):
                        choice = "clean"
                    elif k in ("h", "n", "q", "\x1b"):
                        choice = "skip"
                else:
                    time.sleep(0.05)
            else:
                k = (read_key() or "").lower()
                choice = "clean" if k in ("e", "y") else "skip"
        # Decision made — record it so we don't prompt again next launch.
        _save_config_key("quick_clean_v2_done", True)
        if choice == "skip":
            return

        # ── Delete in Recycle-Bin batches (one COM op per chunk = fast) ──
        ok = err = 0
        freed = 0
        n = len(targets)
        done = 0
        CHUNK = 20
        for start in range(0, n, CHUNK):
            chunk = targets[start:start + CHUNK]
            res = trash_paths_batch([it.path for it in chunk],
                                    sizes={str(it.path): it.size for it in chunk})
            for it in chunk:
                if res.get(str(it.path)) is None:
                    ok += 1
                    freed += it.size
                    it.deleted = True
                else:
                    err += 1
                done += 1
            bar_w = 40
            filled = int(bar_w * done / n) if n else bar_w
            pbody = Text()
            pbody.append((f"\n  Temizleniyor…  {done}/{n}\n\n" if tr else f"\n  Cleaning…  {done}/{n}\n\n"), style="bold white")
            pbody.append("  [" + "█" * filled + "·" * (bar_w - filled) + "]\n", style="bright_cyan")
            pbody.append((f"\n  Boşaltıldı: {human_size(freed)}" if tr else f"\n  Freed: {human_size(freed)}"), style="green")
            live.update(Panel(pbody, title=title, border_style="cyan", padding=(1, 2)))

        res = Text()
        res.append(("\n  ✓ Tamamlandı. " if tr else "\n  ✓ Done. "), style="bold green")
        res.append(human_size(freed), style="bold bright_green")
        res.append((" boşaltıldı  " if tr else " freed  "), style="white")
        res.append((f"({ok} öğe" if tr else f"({ok} items"), style="grey62")
        if err:
            res.append((f", {err} hata" if tr else f", {err} errors"), style="red")
        res.append(")\n", style="grey62")
        live.update(Panel(res, title=title, border_style="green", padding=(1, 2)))
        time.sleep(1.6)


# ---------- Main loop ----------
def run_tui(initial_view: str = "analyze", start_path: Optional[Path] = None,
            use_cache: bool = True) -> None:
    if os.name == "nt":
        os.system("")
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("wmole · disk cleaner")
        except Exception:
            pass

    # Reconcile anything a previous run was in the middle of when it died,
    # before this session can start new destructive work.
    try:
        JOURNAL.recover()
        JOURNAL.prune()
    except Exception:
        pass

    # First launch: offer a one-time quick cleanup before the main UI loads.
    if initial_view == "analyze" and start_path is None:
        try:
            run_first_launch_quick_clean()
        except Exception:
            pass

    whitelist = load_whitelist()
    cfg = load_config()
    analyze_start = start_path or Path(str(cfg.get("analyze_start_path", USER))).expanduser()
    landing_dashboard = (initial_view == "analyze" and start_path is None)
    if initial_view == "purge":
        profile = "purge"
    elif initial_view in ("installer", "installers"):
        profile = "installers"
    elif landing_dashboard:
        # Dashboard estimates reclaimable space from a safe clean scan.
        profile = "clean"
    elif initial_view == "analyze":
        profile = "idle"
    else:
        profile = "full"
    scanner = Scanner(whitelist=whitelist, profile=profile, use_cache=use_cache)
    threading.Thread(target=scanner.run, daemon=True).start()

    apps_cache: List[dict] = []
    opt_cache: List[dict] = [a.__dict__ | {"_obj": a} for a in OPTIMIZE_ACTIONS]
    # we'll keep opt actions as the OptAction objects themselves for render:
    opt_cache = OPTIMIZE_ACTIONS  # type: ignore[assignment]

    use_trash = True
    dry_run = False
    # Claude Code-style slash command palette
    palette_open = False
    palette_query = ""
    palette_cursor = 0
    if initial_view == "status":
        view_stack: List[View] = [View(title="Status", kind="status")]
    elif initial_view == "optimize":
        view_stack = [View(title="Optimize", kind="optimize")]
    elif initial_view == "uninstall":
        apps_cache = list_installed_apps()
        view_stack = [View(title="Uninstall", kind="uninstall")]
    elif initial_view == "purge":
        view_stack = [View(title="Purge Artifacts", kind="cats")]
    elif initial_view in ("installer", "installers"):
        view_stack = [View(title="Installers", kind="cats")]
    elif landing_dashboard:
        view_stack = [View(title=("Gösterge Paneli" if LANG == "tr" else "Dashboard"), kind="dashboard")]
    else:
        view_stack = [View(title=f"Analyze · {analyze_start}", kind="items", category=build_fs_category(analyze_start))]
    cursor = 0
    msg = ""
    if PENDING_UPDATE_FILE.exists():
        try:
            info = json.loads(PENDING_UPDATE_FILE.read_text(encoding="utf-8"))
            ver_tag = info.get("version", "")
            if _parse_semver(ver_tag) > _parse_semver(__version__):
                msg = T_update("ready_prompt", latest=ver_tag).split("\n")[0]
        except Exception:
            pass

    # ── View transition animation state ──
    prev_view_id = None
    transition_frame = 0
    TRANSITION_FRAMES = 4  # frames to spend on transition animation

    with Live(console=console, refresh_per_second=8, screen=True, vertical_overflow="crop") as live:
        last_draw = 0.0
        while True:
            view = view_stack[-1]
            if view.kind in ("cats", "items"):
                rows = view_rows(view, scanner)
            elif view.kind == "optimize":
                rows = list(OPTIMIZE_ACTIONS)
            elif view.kind == "uninstall":
                rows = apps_cache
            else:
                rows = []
            if rows:
                cursor = max(0, min(cursor, len(rows) - 1))

            # Detect view transitions
            current_view_id = (view.kind, view.title)
            if prev_view_id is not None and current_view_id != prev_view_id:
                transition_frame = TRANSITION_FRAMES
            prev_view_id = current_view_id

            # Render with transition effect when transitioning
            if transition_frame > 0:
                # Build transition overlay
                tf = TRANSITION_FRAMES - transition_frame + 1
                tbar_w = 40
                tfilled = int(tbar_w * tf / TRANSITION_FRAMES)
                trans_text = Text()
                trans_text.append("\n\n\n")
                trans_text.append(" " * ((console.size.width or 80) // 2 - tbar_w // 2 - 5))
                trans_text.append("[" + "█" * tfilled + "·" * (tbar_w - tfilled) + "]", style="bright_cyan")
                trans_text.append("\n")
                trans_text.append(" " * ((console.size.width or 80) // 2 - len(view.title) // 2))
                trans_text.append(view.title, style="bold white")
                live.update(Panel(trans_text, border_style="cyan", padding=(3, 2)))
                transition_frame -= 1
                time.sleep(0.04)
                continue

            live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache,
                               palette=(palette_query, palette_cursor) if palette_open else None))
            last_draw = time.time()

            key = None
            for _ in range(20):
                if os.name == "nt" and msvcrt.kbhit():
                    key = read_key()
                    break
                time.sleep(0.05)
                now = time.time()
                active = (not scanner.done) or view.kind in ("status", "dashboard")
                if should_redraw(last_draw, now, scanner_active=active):
                    live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache,
                                   palette=(palette_query, palette_cursor) if palette_open else None))
                    last_draw = now
            if key is None:
                continue
            msg = ""
            up = key.upper() if len(key) == 1 else key
            shifted = key.isalpha() and key.isupper()  # msvcrt returns upper when shift held

            # ---- Slash command palette intercept ----
            forward_palette_key = False
            if palette_open:
                rows_p = filter_palette(palette_query)
                if key == "ESC":
                    palette_open = False; palette_query = ""; palette_cursor = 0
                elif key == "UP":
                    if rows_p: palette_cursor = (palette_cursor - 1) % len(rows_p)
                elif key == "DOWN":
                    if rows_p: palette_cursor = (palette_cursor + 1) % len(rows_p)
                elif key == "ENTER":
                    if rows_p:
                        cmd = rows_p[palette_cursor]
                        palette_open = False; palette_query = ""; palette_cursor = 0
                        action = cmd["action"]
                        if action == "view:dashboard":
                            profile = "clean"
                            scanner = Scanner(whitelist=whitelist, profile="clean", use_cache=use_cache)
                            threading.Thread(target=scanner.run, daemon=True).start()
                            view_stack = [View(title=("Gösterge Paneli" if LANG == "tr" else "Dashboard"), kind="dashboard")]; cursor = 0
                        elif action == "view:analyze-fs":
                            profile = "idle"
                            scanner = Scanner(whitelist=whitelist, profile="idle")
                            threading.Thread(target=scanner.run, daemon=True).start()
                            view_stack = [View(title=f"Analyze · {analyze_start}", kind="items",
                                               category=build_fs_category(analyze_start, live=live, scanner=scanner,
                                                                          view=View(title="", kind="items"), cursor=0,
                                                                          use_trash=use_trash, dry_run=dry_run,
                                                                          apps=apps_cache, opt=opt_cache))]; cursor = 0
                        elif action == "view:cats":
                            profile = "full"
                            scanner = Scanner(whitelist=whitelist, profile="full")
                            threading.Thread(target=scanner.run, daemon=True).start()
                            view_stack = [View(title="Analyze Categories", kind="cats")]; cursor = 0
                        elif action == "view:clean":
                            profile = "clean"
                            scanner = Scanner(whitelist=whitelist, profile="clean")
                            threading.Thread(target=scanner.run, daemon=True).start()
                            view_stack = [View(title="Safe Cleanup", kind="cats")]; cursor = 0
                        elif action == "view:purge":
                            profile = "purge"
                            scanner = Scanner(whitelist=whitelist, profile="purge")
                            threading.Thread(target=scanner.run, daemon=True).start()
                            view_stack = [View(title="Purge Artifacts", kind="cats")]; cursor = 0
                        elif action == "view:installers":
                            profile = "installers"
                            scanner = Scanner(whitelist=whitelist, profile="installers")
                            threading.Thread(target=scanner.run, daemon=True).start()
                            view_stack = [View(title="Installers", kind="cats")]; cursor = 0
                        elif action == "view:uninstall":
                            if not apps_cache: apps_cache = list_installed_apps()
                            view_stack = [View(title="Uninstall", kind="uninstall")]; cursor = 0
                        elif action == "view:optimize":
                            view_stack = [View(title="Optimize", kind="optimize")]; cursor = 0
                        elif action == "view:status":
                            view_stack = [View(title="Status", kind="status")]; cursor = 0
                        elif action == "view:help":
                            view_stack.append(View(title=T("title_help"), kind="help")); cursor = 0
                        elif action == "exec:ports":
                            # Surface a quick snapshot via the status line; full mgmt in CLI.
                            try:
                                rows_pp = list_dev_ports()
                                msg = f"{len(rows_pp)} listening dev port(s) — manage with: wmole ports --kill <port>"
                            except Exception as e:
                                msg = f"ports query failed: {e}"
                        elif action == "exec:doctor":
                            try:
                                summary = doctor_report()["summary"]
                                msg = (f"doctor: ok={summary.get('ok', 0)} warn={summary.get('warn', 0)} "
                                       f"fail={summary.get('fail', 0)} — full report: wmole doctor")
                            except Exception as e:
                                msg = f"doctor failed: {e}"
                        elif action == "exec:undo":
                            try:
                                msg = (f"{len(undo_entries())} restorable item(s) — "
                                       f"run: wmole undo [<id>]")
                            except Exception as e:
                                msg = f"undo query failed: {e}"
                        elif action == "exec:hook":
                            msg = "install the secret-scan hook from a repo: wmole hook install"
                        elif action == "exec:update":
                            try:
                                msg = interactive_tui_update(
                                    live, scanner, view, cursor,
                                    apps_cache, opt_cache, use_trash, dry_run
                                )
                            except Exception as e:
                                msg = f"update failed: {e}"
                        elif action == "exec:quit":
                            return
                        elif action.startswith("key:"):
                            key = action[4:]
                            forward_palette_key = True
                elif key == "BACKSPACE" or key == "\x08":
                    palette_query = palette_query[:-1]
                    palette_cursor = 0
                elif key == "SPACE":
                    palette_query += " "
                    palette_cursor = 0
                elif len(key) == 1 and key.isprintable() and key != "/":
                    palette_query += key
                    palette_cursor = 0
                if not forward_palette_key:
                    continue
                up = key.upper() if len(key) == 1 else key
                shifted = key.isalpha() and key.isupper()

            # Open the palette from anywhere with "/"
            if key == "/":
                palette_open = True
                palette_query = ""
                palette_cursor = 0
                continue

            if up == "Q":
                if len(view_stack) > 1:
                    view_stack.pop(); cursor = 0
                else:
                    break
            elif up == "ESC":
                if len(view_stack) > 1:
                    view_stack.pop(); cursor = 0
                else:
                    break
            elif up == "UP":
                if rows: cursor = (cursor - 1) % len(rows)
            elif up == "DOWN":
                if rows: cursor = (cursor + 1) % len(rows)
            elif up == "K" and not shifted:
                use_trash = not use_trash
                msg = T("permanent_on") if not use_trash else T("permanent_off")
            elif up == "H" and shifted:
                # Open Help (Shift+H to avoid clashing with future single-key uses).
                view_stack.append(View(title=T("title_help"), kind="help"))
                cursor = 0
            elif key == "?":
                view_stack.append(View(title=T("title_help"), kind="help"))
                cursor = 0
            elif key == "\\":
                set_lang("en" if LANG == "tr" else "tr")
                msg = T("lang_switched")
            elif up == "ENTER":
                if not rows: continue
                row = rows[cursor]
                if view.kind == "cats" and isinstance(row, Category):
                    if row.items:
                        view_stack.append(View(title=row.title, kind="items", category=row))
                        cursor = 0
                elif view.kind == "items" and view.category and view.category.key.startswith("fs:") and isinstance(row, Item):
                    target = row.path
                    if row.error == "up":
                        target = row.path
                    if path_exists(target) and target.is_dir():
                        new_cat = build_fs_category(target, live=live, scanner=scanner,
                                                    view=view, cursor=cursor,
                                                    use_trash=use_trash, dry_run=dry_run,
                                                    apps=apps_cache, opt=opt_cache)
                        view_stack.append(View(title=new_cat.title, kind="items", category=new_cat))
                        cursor = 0
                    else:
                        open_in_explorer(target.parent if target.is_file() else target)
                elif view.kind == "optimize":
                    act = row  # OptAction
                    if act.risk == "high":
                        prompt = Text()
                        prompt.append("  ⚠ High risk action. Press Y to continue, anything else to cancel.", style="bold red")
                        live.update(Group(render(scanner, view, cursor, "", use_trash, dry_run, apps_cache, opt_cache), prompt))
                        if read_key().upper() != "Y":
                            msg = "Cancelled."
                            continue
                    msg = f"Running: {act.title}…"
                    live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache,
                               palette=(palette_query, palette_cursor) if palette_open else None))
                    _, msg = run_optimize(act, dry_run=dry_run)
                elif view.kind == "uninstall":
                    app = row
                    uninst = app.get("uninstall")
                    if uninst:
                        try:
                            launch_uninstaller(uninst)
                            leftovers = find_leftover_candidates(app)
                            reg_leftovers = find_registry_leftover_candidates(app)
                            if leftovers:
                                cat = Category(
                                    key="leftovers",
                                    title=f"Leftovers · {app['name']}",
                                    description=f"candidate leftovers (registry: {len(reg_leftovers)})",
                                    icon="🧹",
                                    safe_default=False,
                                    items=leftovers,
                                    scanning=False,
                                )
                                view_stack.append(View(title=cat.title, kind="items", category=cat))
                                cursor = 0
                                msg = f"Launched uninstaller. Found {len(leftovers)} file leftovers and {len(reg_leftovers)} registry candidates."
                            else:
                                msg = f"Launched uninstaller. Registry candidates: {len(reg_leftovers)}."
                        except Exception as e:
                            msg = f"Failed: {e}"
                    else:
                        msg = "No uninstall string recorded."
            elif up == "G" and view.kind == "items" and view.category and view.category.key.startswith("fs:"):
                base = Path(view.category.title)
                threshold = int(load_config().get("large_file_min_mb", 512)) * 1024 * 1024
                # Scan with live progress animation
                def _large_progress(scanned, found):
                    filled = min(24, int(24 * (scanned % 50000) / 50000))
                    bar = "[" + "█" * filled + "·" * (24 - filled) + "]"
                    ptext = Text()
                    ptext.append(f"{bar}  Scanned {scanned} files", style="bright_cyan")
                    ptext.append(f"  Found {found} large", style="bright_yellow")
                    live.update(Group(
                        render(scanner, view, cursor, "", use_trash, dry_run, apps_cache, opt_cache),
                        Panel(ptext, title="Scanning large files…", border_style="cyan", padding=(1, 2)),
                    ))
                large = find_large_files(base, threshold, on_progress=_large_progress)
                cat = Category(
                    key=f"fs-large:{base}",
                    title=f"Large Files · {base}",
                    description=f"files >= {human_size(threshold)}",
                    icon="📄",
                    safe_default=False,
                    scanning=False,
                )
                for row in large:
                    cat.items.append(Item(path=Path(row["path"]), size=int(row["size"]), selected=False))
                view_stack.append(View(title=cat.title, kind="items", category=cat))
                cursor = 0
                msg = f"Found {len(large)} large files (>= {human_size(threshold)})"
            elif up == "V" and view.kind == "items" and view.category and view.category.key.startswith("fs:"):
                drives = build_drive_picker_category()
                view_stack.append(View(title=drives.title, kind="items", category=drives))
                cursor = 0
            elif up == "SPACE" and view.kind in ("cats", "items"):
                if not rows:
                    continue
                row = rows[cursor]
                if isinstance(row, Item):
                    if row.error == "up":
                        continue
                    row.selected = not row.selected
                elif isinstance(row, Category):
                    new = not all(i.selected for i in row.items if not i.deleted)
                    for i in row.items:
                        if not i.deleted:
                            i.selected = new
            elif up == "O":
                if rows:
                    row = rows[cursor]
                    if isinstance(row, Item):
                        open_in_explorer(row.path.parent if row.path.is_file() else row.path)
                    elif isinstance(row, Category) and row.items:
                        open_in_explorer(row.items[0].path)
                    elif view.kind == "uninstall":
                        loc = row.get("install_location")
                        if loc: open_in_explorer(Path(loc))
            elif up == "L" and view.kind == "uninstall" and rows:
                app = rows[cursor]
                leftovers = find_leftover_candidates(app)
                reg_leftovers = find_registry_leftover_candidates(app)
                if leftovers:
                    cat = Category(
                        key="leftovers",
                        title=f"Leftovers · {app['name']}",
                        description=f"candidate app leftovers (registry: {len(reg_leftovers)})",
                        icon="🧹",
                        safe_default=False,
                        items=leftovers,
                        scanning=False,
                    )
                    view_stack.append(View(title=cat.title, kind="items", category=cat))
                    cursor = 0
                    msg = f"Review leftovers before deleting. Registry candidates: {len(reg_leftovers)}."
                else:
                    msg = f"No obvious file leftovers. Registry candidates: {len(reg_leftovers)}."
            elif up in ("C", "D") and view.kind == "dashboard":
                # Quick clean straight from the dashboard: confirm + delete the
                # safe clean-scan selections. Honors the trash/permanent toggle.
                if not scanner.done:
                    msg = "Tarama sürüyor — birazdan tekrar deneyin."
                else:
                    msg = confirm_delete(live, scanner, view, cursor,
                                         use_trash=use_trash,
                                         dry_run=dry_run, apps=apps_cache, opt=opt_cache)
            elif up == "S" and view.kind == "dashboard":
                view_stack.append(View(title="Status", kind="status"))
                cursor = 0
            elif up == "D" and view.kind in ("cats", "items"):
                # `D` honors the current trash/permanent toggle (K).
                msg = confirm_delete(live, scanner, view, cursor,
                                     use_trash=use_trash,
                                     dry_run=dry_run, apps=apps_cache, opt=opt_cache)
            elif up == "R":
                if view.kind in ("cats", "items"):
                    if view.kind == "items" and view.category and view.category.key.startswith("fs:"):
                        base = view.category.title
                        view_stack = [View(title=f"Analyze · {base}", kind="items", category=build_fs_category(Path(base), live=live, scanner=scanner,
                                                                                                              view=view, cursor=cursor,
                                                                                                              use_trash=use_trash, dry_run=dry_run,
                                                                                                              apps=apps_cache, opt=opt_cache))]
                        cursor = 0
                        msg = "Refreshed."
                    else:
                        scanner = Scanner(whitelist=whitelist, profile=profile, use_cache=use_cache)
                        threading.Thread(target=scanner.run, daemon=True).start()
                        view_stack = [View(title="Analyze Disk", kind="cats")]
                        cursor = 0
                        msg = "Rescanning…"
                elif view.kind == "uninstall":
                    apps_cache = list_installed_apps()
                    msg = f"Reloaded {len(apps_cache)} programs."
# ---------- CLI ----------
def status_payload() -> dict:
    """Machine-readable system status. Returns {} if psutil unavailable."""
    if not psutil:
        return {}
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(USER.anchor or 'C:\\')
    net = psutil.net_io_counters()
    dio = psutil.disk_io_counters()
    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    boot = psutil.boot_time()
    sysinfo = platform.uname()
    temps = {}
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures() or {}
        except Exception:
            temps = {}
    return {
        "health": health_score(), "cpu_percent": psutil.cpu_percent(interval=0.3),
        "memory_percent": vm.percent, "memory_used": vm.used, "memory_total": vm.total,
        "disk_percent": du.percent, "disk_free": du.free, "disk_total": du.total,
        "disk_read_bytes": (dio.read_bytes if dio else 0), "disk_write_bytes": (dio.write_bytes if dio else 0),
        "net_up_bytes": net.bytes_sent, "net_down_bytes": net.bytes_recv,
        "uptime_seconds": int(time.time() - boot),
        "battery_percent": (bat.percent if bat else None), "power_plugged": (bat.power_plugged if bat else None),
        "device": {"system": sysinfo.system, "release": sysinfo.release, "node": sysinfo.node},
        "temperature_sensors": {k: [{"label": t.label, "current": t.current} for t in v[:3]] for k, v in temps.items()},
        "cpu_per_core": psutil.cpu_percent(interval=0.0, percpu=True),
        "cpu_count": psutil.cpu_count(logical=True),
    }


# ---------- serve (NDJSON daemon for Tauri GUI) ----------
import threading as _serve_threading


def _serve_emit(lock, event: dict) -> None:
    """Tek satır NDJSON olayı stdout'a atomik yaz."""
    line = json.dumps(event, ensure_ascii=False)
    with lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _serve_scan(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    mode = req.get("mode", "clean")
    paths = [Path(p) for p in req.get("paths", []) if p]
    emit({"id": rid, "ev": "started", "total_hint": None})

    if mode == "analyze":
        target = (paths[0] if paths else USER).expanduser()
        # Stream each child immediately with a capped size computation so that
        # huge roots (e.g. C:\) stay responsive instead of walking the whole
        # drive before showing anything.
        try:
            children = list(target.iterdir()) if target.is_dir() else [target]
        except OSError:
            children = []
        total = len(children)
        total_bytes = 0
        for idx, child in enumerate(children):
            if cancel.is_set():
                emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
                return
            try:
                is_dir = child.is_dir()
                size = quick_size(child)
            except OSError:
                continue
            total_bytes += size
            emit({"id": rid, "ev": "item", "path": str(child),
                  "name": child.name, "size": size,
                  "kind": "dir" if is_dir else "file", "selected": False})
            emit({"id": rid, "ev": "progress", "done": idx + 1,
                  "total": total, "label": child.name})
        emit({"id": rid, "ev": "done", "ok": True,
              "summary": {"count": total, "bytes": total_bytes}})
        return

    if mode == "large":
        target = paths[0] if paths else USER
        min_large = int(load_config().get("large_file_min_mb", 512)) * 1024 * 1024
        for f in find_large_files(target, min_large):
            if cancel.is_set():
                emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
                return
            emit({"id": rid, "ev": "item", "path": str(f["path"]),
                  "name": Path(f["path"]).name, "size": int(f["size"]),
                  "kind": "large-file", "selected": False})
        emit({"id": rid, "ev": "done", "ok": True})
        return

    profile = {"clean": "clean", "purge": "purge",
               "installers": "installers", "categories": "full"}.get(mode, "clean")

    # Stream every artifact the moment it has been sized. Previously the whole
    # scan ran as one blocking call that emitted nothing and only looked at
    # `cancel` after it returned, so the UI sat at 0/0 and Cancel did nothing.
    streamed: set = set()

    def on_item(cat, it) -> None:
        streamed.add(str(it.path))
        emit({"id": rid, "ev": "item", "path": str(it.path),
              "name": it.path.name, "size": int(it.size),
              "kind": cat.key, "category": cat.title,
              "truncated": bool(getattr(it, "truncated", False)),
              "selected": bool(it.selected)})

    def on_progress(done: int, label: str) -> None:
        emit({"id": rid, "ev": "progress", "done": done, "total": 0,
              "label": label})

    scanner = Scanner(whitelist=load_whitelist(), profile=profile,
                      roots=paths or None, use_cache=not req.get("no_cache"),
                      on_item=on_item, on_progress=on_progress, cancel=cancel)
    scanner.run()
    if cancel.is_set():
        emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
        return
    report = collect_selected_targets(scanner.categories, estimated=not paths)
    count = len(streamed)
    total = report.get("total", 0)
    # Profiles that do not stream (clean/installers/full) still emit here; any
    # item already streamed above is skipped so the UI never double-counts.
    for cat in scanner.categories:
        for it in cat.items:
            if cancel.is_set():
                emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
                return
            if it.deleted or str(it.path) in streamed:
                continue
            count += 1
            emit({"id": rid, "ev": "item", "path": str(it.path),
                  "name": it.path.name, "size": int(it.size),
                  "kind": cat.key, "category": cat.title,
                  "truncated": bool(getattr(it, "truncated", False)),
                  "selected": bool(it.selected)})
    emit({"id": rid, "ev": "done", "ok": True,
          "summary": {"count": count, "bytes": total}})


def _serve_delete(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    targets = [Path(p) for p in req.get("targets", []) if p]
    use_trash = not bool(req.get("permanent"))
    dry_run = bool(req.get("dry_run"))
    emit({"id": rid, "ev": "started", "total_hint": len(targets)})
    ok = err = 0
    for idx, path in enumerate(targets):
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True,
                  "summary": {"ok": ok, "err": err}})
            return
        error = delete_path(path, use_trash=use_trash, dry_run=dry_run)
        if error:
            err += 1
            emit({"id": rid, "ev": "item_result", "path": str(path),
                  "ok": False, "error": error})
        else:
            ok += 1
            emit({"id": rid, "ev": "item_result", "path": str(path),
                  "ok": True, "trashed": use_trash, "dry_run": dry_run})
        emit({"id": rid, "ev": "progress", "done": idx + 1, "total": len(targets),
              "label": str(path)})
    emit({"id": rid, "ev": "done", "ok": True,
          "summary": {"ok": ok, "err": err}})


def _serve_uninstall_list(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    query = req.get("query", "")
    emit({"id": rid, "ev": "started", "total_hint": None})
    apps = filter_apps(list_installed_apps(), query=query, limit=req.get("limit"))
    for app in apps:
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        size = int(app.get("estimated_size_kb") or 0) * 1024
        emit({"id": rid, "ev": "item", "path": app.get("uninstall_key", ""),
              "name": app.get("name", ""), "size": size, "kind": "app",
              "publisher": app.get("publisher", ""), "version": app.get("version", ""),
              "install_location": app.get("install_location", ""),
              "uninstall": app.get("uninstall", ""), "selected": False,
              "app": app})
    emit({"id": rid, "ev": "done", "ok": True, "summary": {"count": len(apps)}})


def _serve_leftovers(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    app = req.get("app") or {"name": req.get("name", "")}
    emit({"id": rid, "ev": "started", "total_hint": None})
    files = find_leftover_candidates(app)
    for it in files[:200]:
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        emit({"id": rid, "ev": "item", "path": str(it.path), "name": it.path.name,
              "size": int(it.size), "kind": "leftover-file", "selected": False})
    for key in find_registry_leftover_candidates(app):
        emit({"id": rid, "ev": "item", "path": key, "name": key,
              "size": 0, "kind": "leftover-reg", "selected": False})
    emit({"id": rid, "ev": "done", "ok": True})


def _serve_uninstall_run(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    requested = req.get("uninstall", "")
    key = req.get("uninstall_key", "")
    emit({"id": rid, "ev": "started", "total_hint": None})
    # Never execute a client-supplied command string directly: resolve the
    # uninstall command from the registry inventory so the bridge can only
    # launch real, installed uninstallers.
    uninst = ""
    for app in list_installed_apps():
        if key and app.get("uninstall_key") == key:
            uninst = app.get("uninstall", "")
            break
        if requested and app.get("uninstall") == requested:
            uninst = requested
            break
    if not uninst:
        emit({"id": rid, "ev": "error", "code": "no_uninstall_string",
              "message": "uninstall string not found in registry inventory"})
        emit({"id": rid, "ev": "done", "ok": False})
        return
    try:
        launch_uninstaller(uninst)
        emit({"id": rid, "ev": "item_result", "path": uninst, "ok": True})
        emit({"id": rid, "ev": "done", "ok": True})
    except Exception as exc:
        emit({"id": rid, "ev": "error", "code": "launch_failed", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})


def _serve_optimize_list(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    emit({"id": rid, "ev": "started", "total_hint": None})
    for act in OPTIMIZE_ACTIONS:
        emit({"id": rid, "ev": "item", "path": act.key, "name": act.title,
              "size": 0, "kind": "optimize", "description": act.description,
              "risk": act.risk, "requires_admin": act.requires_admin,
              "selected": False})
    emit({"id": rid, "ev": "done", "ok": True})


def _serve_optimize_run(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    keys = req.get("keys") or ([req["key"]] if req.get("key") else [])
    dry_run = bool(req.get("dry_run"))
    by_key = {a.key: a for a in OPTIMIZE_ACTIONS}
    emit({"id": rid, "ev": "started", "total_hint": len(keys)})
    for idx, key in enumerate(keys):
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        act = by_key.get(key)
        if not act:
            emit({"id": rid, "ev": "item_result", "path": key, "ok": False,
                  "error": "unknown action"})
            continue
        ok, msg = run_optimize(act, dry_run=dry_run)
        emit({"id": rid, "ev": "item_result", "path": key, "ok": ok,
              "message": msg, "title": act.title})
        emit({"id": rid, "ev": "progress", "done": idx + 1, "total": len(keys),
              "label": act.title})
    emit({"id": rid, "ev": "done", "ok": True})


def _serve_ports_list(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    emit({"id": rid, "ev": "started", "total_hint": None})
    rows = list_dev_ports(include_all=bool(req.get("all_binds")))
    for r in rows:
        bind_ip = r.get("ip", "")
        emit({"id": rid, "ev": "item",
              "path": f"{r['proto']}|{bind_ip}|{r['port']}|{r.get('pid') or ''}",
              "name": f"{r['proto']}/{r['port']}", "size": 0, "kind": "port",
              "port": r["port"], "pid": r.get("pid"), "proto": r["proto"],
              "process": r.get("process", ""), "ip": bind_ip,
              "project": r.get("project", ""),
              "hint": r.get("hint", ""), "selected": False})
    emit({"id": rid, "ev": "done", "ok": True, "summary": {"count": len(rows)}})


def _serve_ports_kill(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    pids = req.get("pids") or ([req["pid"]] if req.get("pid") else [])
    dry_run = bool(req.get("dry_run"))
    emit({"id": rid, "ev": "started", "total_hint": len(pids)})
    for idx, pid in enumerate(pids):
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        try:
            pid_num = int(pid)
        except (TypeError, ValueError):
            emit({"id": rid, "ev": "item_result", "path": str(pid), "ok": False,
                  "message": f"invalid pid: {pid!r}"})
            continue
        ok, msg = kill_pid(pid_num, dry_run=dry_run)
        log_operation("port_kill", Path(f"pid:{pid_num}"), 0, msg)
        emit({"id": rid, "ev": "item_result", "path": str(pid_num), "ok": ok,
              "message": msg})
        emit({"id": rid, "ev": "progress", "done": idx + 1, "total": len(pids),
              "label": f"pid {pid}"})
    emit({"id": rid, "ev": "done", "ok": True})


def _serve_update(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    dry_run = bool(req.get("dry_run"))
    emit({"id": rid, "ev": "started", "total_hint": None})
    payload: dict = {}
    cli_update(json_out=False, yes=bool(req.get("yes")),
               dry_run=dry_run, quiet=True, sink=payload)
    emit({"id": rid, "ev": "done", "ok": True, "payload": payload})


def _serve_remove(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    dry_run = bool(req.get("dry_run"))
    emit({"id": rid, "ev": "started", "total_hint": None})
    payload: dict = {}
    cli_remove(dry_run=dry_run, json_out=False, sink=payload)
    emit({"id": rid, "ev": "done", "ok": True, "payload": payload})


def _serve_cleanup_history(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    hist = read_cleanup_history(limit=int(req.get("limit", 8)))
    emit({"id": rid, "ev": "done", "ok": True, "payload": hist})


def _serve_drives(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    drives = []
    if psutil:
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                drives.append({
                    "device": part.device, "mountpoint": part.mountpoint,
                    "fstype": part.fstype, "total": u.total, "used": u.used,
                    "free": u.free, "percent": u.percent,
                })
            except Exception:
                continue
    emit({"id": rid, "ev": "done", "ok": True, "payload": {"drives": drives}})


def _serve_open_path(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    target = req.get("path", "")
    try:
        p = Path(target)
        if p.is_dir():
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer", "/select,", str(p)])
        emit({"id": rid, "ev": "done", "ok": True})
    except Exception as exc:
        emit({"id": rid, "ev": "error", "code": "open_failed", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})


def _read_list_file(path: Path) -> list:
    out = []
    try:
        if path_exists(path):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
    except Exception:
        pass
    return out


def _write_list_file(path: Path, items: list) -> None:
    WMOLE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("# one path per line\n" + "\n".join(items) + "\n", encoding="utf-8")


def _serve_settings_get(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    emit({"id": rid, "ev": "done", "ok": True, "payload": {
        "config": load_config(),
        "whitelist": _read_list_file(WHITELIST_FILE),
        "denylist": _read_list_file(DENYLIST_FILE),
        "purge_paths": _read_list_file(PURGE_PATHS_FILE),
    }})


def _serve_settings_set(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    files = {"whitelist": WHITELIST_FILE, "denylist": DENYLIST_FILE,
             "purge_paths": PURGE_PATHS_FILE}
    for key, fp in files.items():
        if key in req and isinstance(req[key], list):
            _write_list_file(fp, [str(x) for x in req[key]])
    for k, v in (req.get("config") or {}).items():
        _save_config_key(k, v)
    _serve_settings_get(req, emit, cancel)


def _serve_is_admin(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    admin = False
    try:
        import ctypes
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass
    emit({"id": rid, "ev": "done", "ok": True, "payload": {"is_admin": admin}})


def _serve_completion_install(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    try:
        script = powershell_completion_script()
        WMOLE_DIR.mkdir(parents=True, exist_ok=True)
        COMPLETION_FILE.write_text(script + "\n", encoding="utf-8")
        emit({"id": rid, "ev": "done", "ok": True,
              "payload": {"path": str(COMPLETION_FILE)}})
    except Exception as exc:
        emit({"id": rid, "ev": "error", "code": "completion_failed", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})


SCHED_TASK_NAME = "wmole-weekly-clean"


def _serve_schedule_get(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    enabled = False
    detail = ""
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", SCHED_TASK_NAME],
                           capture_output=True, text=True, timeout=15)
        enabled = r.returncode == 0
        detail = (r.stdout or "").strip()[:400]
    except Exception:
        pass
    emit({"id": rid, "ev": "done", "ok": True,
          "payload": {"enabled": enabled, "detail": detail}})


def _serve_schedule_set(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    day = req.get("day", "SUN")
    time_s = req.get("time", "20:00")
    exe = sys.executable
    # serve modunda değil; clean modunu --yes ile çalıştır
    if getattr(sys, "frozen", False):
        cmd_str = f'"{exe}" clean --yes'
    else:
        cmd_str = f'"{exe}" "{os.path.abspath(__file__)}" clean --yes'
    try:
        r = subprocess.run([
            "schtasks", "/create", "/f", "/tn", SCHED_TASK_NAME,
            "/sc", "weekly", "/d", day, "/st", time_s, "/tr", cmd_str,
        ], capture_output=True, text=True, timeout=20)
        ok = r.returncode == 0
        emit({"id": rid, "ev": "done", "ok": ok,
              "payload": {"message": (r.stdout or r.stderr).strip()}})
    except Exception as exc:
        emit({"id": rid, "ev": "error", "code": "schedule_failed", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})


def _serve_schedule_clear(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    try:
        r = subprocess.run(["schtasks", "/delete", "/f", "/tn", SCHED_TASK_NAME],
                           capture_output=True, text=True, timeout=15)
        emit({"id": rid, "ev": "done", "ok": r.returncode == 0,
              "payload": {"message": (r.stdout or r.stderr).strip()}})
    except Exception as exc:
        emit({"id": rid, "ev": "error", "code": "schedule_failed", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})


def _startup_entries() -> list:
    """Windows başlangıç girdileri: Run registry anahtarları + Startup klasörü."""
    entries = []
    try:
        import winreg
        hives = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
        ]
        for hive, sub, label in hives:
            try:
                key = winreg.OpenKey(hive, sub)
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, i)
                        entries.append({"name": name, "command": val,
                                        "location": f"{label}\\Run", "enabled": True})
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except OSError:
                pass
    except (ImportError, OSError):
        pass
    startup_dir = USER / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    try:
        if startup_dir.is_dir():
            for f in startup_dir.iterdir():
                entries.append({"name": f.name, "command": str(f),
                                "location": "Startup Folder", "enabled": True})
    except OSError:
        pass
    return entries


def _serve_startup_list(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    emit({"id": rid, "ev": "started", "total_hint": None})
    for e in _startup_entries():
        emit({"id": rid, "ev": "item", "path": e["command"], "name": e["name"],
              "size": 0, "kind": "startup", "location": e["location"],
              "selected": False})
    emit({"id": rid, "ev": "done", "ok": True})


def _serve_startup_disable(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    name = req.get("name", "")
    location = req.get("location", "")
    emit({"id": rid, "ev": "started", "total_hint": None})
    try:
        if location.endswith("Run"):
            import winreg
            hive = winreg.HKEY_CURRENT_USER if location.startswith("HKCU") else winreg.HKEY_LOCAL_MACHINE
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(key, name)
            winreg.CloseKey(key)
            emit({"id": rid, "ev": "item_result", "path": name, "ok": True})
        elif location == "Startup Folder":
            err = delete_path(Path(req.get("path", "")), use_trash=True, dry_run=False)
            emit({"id": rid, "ev": "item_result", "path": name, "ok": err is None,
                  "error": err})
        emit({"id": rid, "ev": "done", "ok": True})
    except OSError as exc:
        emit({"id": rid, "ev": "error", "code": "startup_failed", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})


def _serve_processes_list(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    emit({"id": rid, "ev": "started", "total_hint": None})
    procs = []
    if psutil:
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            try:
                info = p.info
                mem = info["memory_info"].rss if info.get("memory_info") else 0
                procs.append({"pid": info["pid"], "name": info.get("name") or "?",
                              "mem": mem, "cpu": info.get("cpu_percent") or 0.0})
            except (psutil.Error, OSError, KeyError):
                continue
    procs.sort(key=lambda x: x["mem"], reverse=True)
    protected_names = {str(v).lower() for v in load_config().get("protected_processes", [])}
    for p in procs[:300]:
        emit({"id": rid, "ev": "item", "path": str(p["pid"]), "name": p["name"],
              "size": p["mem"], "kind": "process", "pid": p["pid"],
              "cpu": p["cpu"], "protected": p["name"].lower() in protected_names,
              "selected": False})
    emit({"id": rid, "ev": "done", "ok": True, "summary": {"count": len(procs)}})


def _file_digest(path: Path, quick: bool = False, edge: int = 65536) -> Optional[str]:
    """blake2b digest of a file (no crypto intent, just fast bucketing).

    quick=True hashes only the first+last `edge` bytes, so obvious mismatches
    are eliminated without reading whole files.
    """
    h = hashlib.blake2b(digest_size=16)
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if quick and size > 2 * edge:
                h.update(fh.read(edge))
                fh.seek(-edge, os.SEEK_END)
                h.update(fh.read(edge))
            else:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _duplicate_groups(paths: List[Path]) -> Dict[str, List[Path]]:
    """Group same-size files by content using a quick pre-hash pass first."""
    prelim: Dict[str, List[Path]] = {}
    for fp in paths:
        digest = _file_digest(fp, quick=True)
        if digest is not None:
            prelim.setdefault(digest, []).append(fp)
    groups: Dict[str, List[Path]] = {}
    for candidates in prelim.values():
        if len(candidates) < 2:
            continue
        for fp in candidates:
            digest = _file_digest(fp, quick=False)
            if digest is not None:
                groups.setdefault(digest, []).append(fp)
    return {digest: rows for digest, rows in groups.items() if len(rows) >= 2}


def _serve_duplicates(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    roots = [Path(p) for p in req.get("paths", []) if p] or [USER]
    min_size = int(req.get("min_size", 1024 * 1024))  # 1MB default
    emit({"id": rid, "ev": "started", "total_hint": None})
    by_size: dict = {}
    scanned = 0
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            prune_reparse_dirs(dirpath, dirs)
            if cancel.is_set():
                emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
                return
            for fn in files:
                fp = Path(dirpath) / fn
                try:
                    sz = fp.stat().st_size
                except OSError:
                    continue
                if sz >= min_size:
                    by_size.setdefault(sz, []).append(fp)
            scanned += 1
            if scanned % 200 == 0:
                emit({"id": rid, "ev": "progress", "done": scanned, "total": 0,
                      "label": dirpath})
    groups = 0
    for sz, paths in by_size.items():
        if len(paths) < 2:
            continue
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        hashes = _duplicate_groups(paths)
        for digest, dups in hashes.items():
            groups += 1
            for fp in dups:
                emit({"id": rid, "ev": "item", "path": str(fp), "name": fp.name,
                      "size": sz, "kind": "duplicate", "group": digest[:8],
                      "selected": False})
    emit({"id": rid, "ev": "done", "ok": True, "summary": {"groups": groups}})


SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic-secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?([^\s'\"]{12,})")),
]
SCAN_SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", ".venv", "venv", "__pycache__"}


def scan_secrets(root: Path, cancel=None, max_files: int = 5000) -> dict:
    findings: list[dict] = []
    scanned = 0
    if not path_exists(root):
        return {"root": str(root), "scanned": 0, "findings": [], "error": "path not found"}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP_DIRS]
        prune_reparse_dirs(dirpath, dirs)
        if cancel is not None and cancel.is_set():
            break
        for filename in files:
            if scanned >= max_files:
                break
            path = Path(dirpath) / filename
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw[:4096]:
                    continue
                text = raw.decode("utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for line_no, line in enumerate(text.splitlines(), 1):
                for kind, pattern in SECRET_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        value = match.group(0)
                        preview = value[:4] + "..." + value[-4:] if len(value) > 10 else "[redacted]"
                        findings.append({
                            "type": kind, "path": str(path), "line": line_no,
                            "preview": preview, "severity": "high",
                        })
                        break
        if scanned >= max_files:
            break
    log_security_event("secret-scan", {"root": str(root), "scanned": scanned,
                                       "findings": len(findings)})
    return {"root": str(root), "scanned": scanned, "findings": findings,
            "truncated": scanned >= max_files}


def _run_audit(command: list[str], cwd: Path, timeout: int = 90) -> dict:
    executable = shutil.which(command[0])
    if not executable:
        return {"tool": command[0], "status": "unavailable", "output": "tool not installed"}
    try:
        # Windows: npm/yarn/pip are .cmd shims; CreateProcess can't resolve the
        # bare name, so always run the path shutil.which() resolved.
        resolved = [executable, *command[1:]]
        result = subprocess.run(resolved, cwd=str(cwd), capture_output=True, text=True,
                                timeout=timeout, shell=False)
        output = (result.stdout or result.stderr or "").strip()
        return {"tool": command[0], "status": "ok" if result.returncode == 0 else "findings",
                "code": result.returncode, "output": output[:20000]}
    except subprocess.TimeoutExpired:
        return {"tool": command[0], "status": "timeout", "output": "audit timed out"}
    except OSError as exc:
        return {"tool": command[0], "status": "error", "output": str(exc)}


def supply_chain_audit(root: Path) -> dict:
    manifests: list[dict] = []
    project_roots: dict[str, set[str]] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP_DIRS]
        prune_reparse_dirs(dirpath, dirs)
        for filename in files:
            if filename not in {"package.json", "package-lock.json", "requirements.txt",
                                "pyproject.toml", "Cargo.toml", "Cargo.lock"}:
                continue
            path = Path(dirpath) / filename
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                manifests.append({"path": str(path), "name": filename, "sha256": digest})
                project_roots.setdefault(str(path.parent), set()).add(filename)
            except OSError:
                continue
            if len(manifests) >= 200:
                break
        if len(manifests) >= 200:
            break
    audits: list[dict] = []
    for project, names in list(project_roots.items())[:20]:
        cwd = Path(project)
        if "package-lock.json" in names:
            audits.append({"project": project, **_run_audit(["npm", "audit", "--json"], cwd)})
        if "Cargo.lock" in names:
            audits.append({"project": project, **_run_audit(["cargo", "audit", "--json"], cwd)})
        if "requirements.txt" in names or "pyproject.toml" in names:
            audits.append({"project": project, **_run_audit(["pip-audit", "--format", "json"], cwd)})
    log_security_event("supply-chain-audit", {"root": str(root), "manifests": len(manifests),
                                              "audits": len(audits)})
    return {"root": str(root), "sbom": manifests, "audits": audits}


def _serve_security_overview(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    purge_expired_quarantine()
    entries = _quarantine_entries()
    logs, valid = read_security_log(limit=int(req.get("limit", 100)))
    emit({"id": rid, "ev": "done", "ok": True, "payload": {
        "quarantine": entries, "security_log": logs, "log_valid": valid,
        "config": load_config(),
    }})


def _serve_quarantine_restore(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    entry_id = str(req.get("entry_id", ""))
    error = restore_quarantine(entry_id)
    emit({"id": rid, "ev": "done", "ok": error is None,
          "payload": {"error": error or "", "id": entry_id}})


def _serve_secret_scan(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    root = Path(str(req.get("path") or USER)).expanduser()
    emit({"id": rid, "ev": "started", "total_hint": None})
    payload = scan_secrets(root, cancel=cancel)
    emit({"id": rid, "ev": "done", "ok": True, "payload": payload})


def _serve_supply_audit(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    root = Path(str(req.get("path") or USER)).expanduser()
    emit({"id": rid, "ev": "started", "total_hint": None})
    payload = supply_chain_audit(root)
    emit({"id": rid, "ev": "done", "ok": True, "payload": payload})


def _serve_explain(req: dict, emit, cancel) -> None:
    """Answer the six questions for one or many candidates.

    Purely table-driven, so a whole visible page can be explained in one call
    without touching the filesystem more than the risk score already does.
    """
    rid = req.get("id")
    rows = req.get("items") or []
    if req.get("path"):
        rows = [{"path": req["path"], "kind": req.get("kind", "")}]
    want_risk = req.get("risk", True)
    check_git = bool(req.get("check_git", False))
    payload = []
    for row in rows:
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        p = Path(str(row.get("path", "")))
        kind = str(row.get("kind", ""))
        entry = {"path": str(p), "kind": kind,
                 "explain": explain_item(p, kind).as_dict()}
        if want_risk:
            entry["risk"] = risk_score(p, kind, check_git=check_git).as_dict()
        payload.append(entry)
    emit({"id": rid, "ev": "done", "ok": True, "payload": {"items": payload}})


def _serve_disk_facts(req: dict, emit, cancel) -> None:
    """Report logical vs. actually-allocated bytes for the given paths."""
    rid = req.get("id")
    paths = [Path(p) for p in req.get("paths", []) if p]
    budget = float(req.get("budget_seconds", 8.0))
    rows = []
    for p in paths:
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        facts = disk_facts(p, max_seconds=budget)
        rows.append({"path": str(p), **facts.as_dict()})
        emit({"id": rid, "ev": "item", "path": str(p), **facts.as_dict()})
    total = DiskFacts()
    for row in rows:
        total.logical += row["logical"]
        total.on_disk += row["on_disk"]
        total.placeholder_bytes += row["placeholder_bytes"]
        total.placeholder_files += row["placeholder_files"]
        total.files += row["files"]
        total.truncated = total.truncated or row["truncated"]
    emit({"id": rid, "ev": "done", "ok": True,
          "payload": {"items": rows, "total": total.as_dict()}})


def _serve_journal(req: dict, emit, cancel) -> None:
    """Inspect or reconcile the write-ahead operation journal."""
    rid = req.get("id")
    if req.get("recover"):
        outcomes = JOURNAL.recover()
        JOURNAL.prune()
        emit({"id": rid, "ev": "done", "ok": True,
              "payload": {"recovered": outcomes}})
        return
    emit({"id": rid, "ev": "done", "ok": True,
          "payload": {"open": JOURNAL.open_entries()}})


def _serve_quarantine_verify(req: dict, emit, cancel) -> None:
    """Re-check quarantined content against the digest taken at delete time."""
    rid = req.get("id")
    wanted = req.get("entry_id")
    rows = []
    for entry in _quarantine_entries():
        if wanted and entry.get("id") != wanted:
            continue
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
            return
        stored = Path(entry.get("stored", ""))
        if not path_exists(stored):
            problem = "quarantined content is missing"
        else:
            problem = verify_digest(stored, entry.get("digest", ""),
                                    entry.get("digest_algo", ""))
        rows.append({"id": entry.get("id"), "original": entry.get("original"),
                     "algo": entry.get("digest_algo") or "",
                     "partial": bool(entry.get("digest_partial")),
                     "ok": problem is None, "problem": problem or ""})
    emit({"id": rid, "ev": "done", "ok": True, "payload": {"entries": rows}})


def _serve_handle(req: dict, emit, cancels: dict) -> None:
    """Tek bir isteği işle. emit(event_dict) çağrılır."""
    rid = req.get("id")
    op = req.get("op")
    cancel = _serve_threading.Event()
    if rid is not None:
        cancels[rid] = cancel
    try:
        if op == "ping":
            emit({"id": rid, "ev": "done", "ok": True})
        elif op == "status":
            emit({"id": rid, "ev": "done", "ok": True, "payload": status_payload()})
        elif op == "scan":
            _serve_scan(req, emit, cancel)
        elif op == "delete":
            _serve_delete(req, emit, cancel)
        elif op == "uninstall_list":
            _serve_uninstall_list(req, emit, cancel)
        elif op == "leftovers":
            _serve_leftovers(req, emit, cancel)
        elif op == "uninstall_run":
            _serve_uninstall_run(req, emit, cancel)
        elif op == "optimize_list":
            _serve_optimize_list(req, emit, cancel)
        elif op == "optimize_run":
            _serve_optimize_run(req, emit, cancel)
        elif op == "ports_list":
            _serve_ports_list(req, emit, cancel)
        elif op == "ports_kill":
            _serve_ports_kill(req, emit, cancel)
        elif op == "update":
            _serve_update(req, emit, cancel)
        elif op == "remove":
            _serve_remove(req, emit, cancel)
        elif op == "cleanup_history":
            _serve_cleanup_history(req, emit, cancel)
        elif op == "drives":
            _serve_drives(req, emit, cancel)
        elif op == "open_path":
            _serve_open_path(req, emit, cancel)
        elif op == "settings_get":
            _serve_settings_get(req, emit, cancel)
        elif op == "settings_set":
            _serve_settings_set(req, emit, cancel)
        elif op == "is_admin":
            _serve_is_admin(req, emit, cancel)
        elif op == "completion_install":
            _serve_completion_install(req, emit, cancel)
        elif op == "schedule_get":
            _serve_schedule_get(req, emit, cancel)
        elif op == "schedule_set":
            _serve_schedule_set(req, emit, cancel)
        elif op == "schedule_clear":
            _serve_schedule_clear(req, emit, cancel)
        elif op == "startup_list":
            _serve_startup_list(req, emit, cancel)
        elif op == "startup_disable":
            _serve_startup_disable(req, emit, cancel)
        elif op == "processes_list":
            _serve_processes_list(req, emit, cancel)
        elif op == "duplicates":
            _serve_duplicates(req, emit, cancel)
        elif op == "security_overview":
            _serve_security_overview(req, emit, cancel)
        elif op == "quarantine_restore":
            _serve_quarantine_restore(req, emit, cancel)
        elif op == "secret_scan":
            _serve_secret_scan(req, emit, cancel)
        elif op == "supply_audit":
            _serve_supply_audit(req, emit, cancel)
        elif op == "explain":
            _serve_explain(req, emit, cancel)
        elif op == "disk_facts":
            _serve_disk_facts(req, emit, cancel)
        elif op == "journal":
            _serve_journal(req, emit, cancel)
        elif op == "quarantine_verify":
            _serve_quarantine_verify(req, emit, cancel)
        else:
            emit({"id": rid, "ev": "error", "code": "unknown_op",
                  "message": f"unknown op: {op}"})
            emit({"id": rid, "ev": "done", "ok": False})
    except Exception as exc:  # bir istek hatası döngüyü düşürmesin
        emit({"id": rid, "ev": "error", "code": "exception", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})
    finally:
        cancels.pop(rid, None)


def cmd_serve() -> None:
    """Kalıcı NDJSON istek/olay döngüsü. stdin'den satır-başına-bir-JSON okur."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    out_lock = _serve_threading.Lock()
    cancels: dict = {}  # rid -> threading.Event
    workers: list = []

    # Reconcile anything that was in flight when a previous run was killed,
    # before the UI can start issuing new destructive work.
    try:
        recovered = JOURNAL.recover()
        JOURNAL.prune()
    except Exception:
        recovered = []
    _serve_emit(out_lock, {"id": None, "ev": "ready", "recovered": recovered})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _serve_emit(out_lock, {"id": None, "ev": "error",
                                   "code": "bad_json", "message": raw[:200]})
            continue
        rid = req.get("id")
        if req.get("op") == "cancel":
            ev = cancels.get(rid)
            if ev:
                ev.set()
            continue
        worker = _serve_threading.Thread(
            target=_serve_handle,
            args=(req, lambda e: _serve_emit(out_lock, e), cancels),
            daemon=True,
        )
        worker.start()
        workers.append(worker)
        workers = [w for w in workers if w.is_alive()]

    # stdin kapandı: çalışan worker'ların olaylarını yazmasını bekle (graceful shutdown).
    for w in workers:
        w.join(timeout=120)


def cli_clean(dry_run: bool, json_out: bool, roots: Optional[List[Path]] = None,
              whitelist: Optional[List[Path]] = None, use_cache: bool = True) -> None:
    """Headless: scan, pick the safe_default categories, ask once, delete."""
    wl = whitelist if whitelist is not None else load_whitelist()
    s = Scanner(whitelist=wl, profile="clean", roots=roots, use_cache=use_cache)
    s.run()
    report = collect_selected_targets(s.categories, estimated=roots is None)
    target_paths = {row["path"] for row in report["targets"]}
    targets = [it for c in s.categories for it in c.items if it.selected and str(it.path) in target_paths]
    total = report["total"]
    if json_out:
        report["dry_run"] = dry_run
        report["json_preview_only"] = True
        print(json.dumps(report, indent=2))
        return
    print(f"{len(targets)} item(s), {human_size(total)} — {'dry-run' if dry_run else 'delete to Recycle Bin'}? [y/N] ", end="")
    if input().strip().lower() != "y":
        print("Cancelled."); return
    ok = err = 0
    for t in targets:
        e = delete_path(t.path, use_trash=True, dry_run=dry_run)
        if e: err += 1
        else: ok += 1
    print(f"Done. {ok} ok, {err} errors.")


def cli_categories(categories: List[Category], dry_run: bool, json_out: bool, label: str) -> None:
    report = collect_selected_targets(categories, estimated=False)
    target_paths = {row["path"] for row in report["targets"]}
    targets = [item for cat in categories for item in cat.items if item.selected and not item.deleted and str(item.path) in target_paths]
    total = report["total"]
    if json_out:
        report["mode"] = label
        report["dry_run"] = dry_run
        report["json_preview_only"] = True
        print(json.dumps(report, indent=2))
        return
    if not targets:
        print(f"No {label} targets found.")
        return
    print(f"{label}: {len(targets)} item(s), {human_size(total)}")
    for item in targets[:30]:
        print(f"  {human_size(item.size):>10}  {item.path}")
    if len(targets) > 30:
        print(f"  ... {len(targets) - 30} more")
    print(f"{'DRY-RUN: preview only' if dry_run else 'Move selected items to Recycle Bin'}? [y/N] ", end="")
    if input().strip().lower() != "y":
        print("Cancelled."); return
    ok = err = 0
    for item in targets:
        e = delete_path(item.path, use_trash=True, dry_run=dry_run)
        if e:
            err += 1
        else:
            ok += 1
    print(f"Done. {ok} ok, {err} errors.")


def cli_analyze(path: Path, json_out: bool) -> None:
    min_large = int(load_config().get("large_file_min_mb", 512)) * 1024 * 1024
    result = analyze_path(path, large_file_min=min_large)
    if json_out:
        print(json.dumps(result, indent=2))
        return
    print(f"Analyze: {result['path']}")
    print(f"Total: {human_size(result['total_size'])} across {result['total_files']} file(s)")
    for entry in result["entries"][:40]:
        kind = "dir " if entry["is_dir"] else "file"
        print(f"  {human_size(entry['size']):>10}  {kind}  {entry['name']}")


def normalize_app_tokens(*parts: str) -> List[str]:
    stop = {"inc", "llc", "ltd", "corp", "corporation", "company", "gmbh", "x64", "x86", "64bit", "32bit"}
    tokens: List[str] = []
    for part in parts:
        cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in part)
        for token in cleaned.split():
            if len(token) < 3 or token.isdigit() or token in stop:
                continue
            tokens.append(token)
    compact = "".join(tokens)
    out = []
    for token in tokens + ([compact] if len(compact) >= 4 else []):
        if token not in out:
            out.append(token)
    return out


def is_leftover_match(path: Path, tokens: List[str]) -> bool:
    compact_name = "".join(ch.lower() for ch in path.stem if ch.isalnum())
    if not compact_name:
        return False
    for token in tokens:
        if compact_name == token:
            return True
        # Prefix matches only when BOTH sides are long enough; this prevents
        # short tokens (git, vlc, obs) from matching unrelated folders.
        if len(token) >= 5 and len(compact_name) >= 5 and (
            compact_name.startswith(token) or token.startswith(compact_name)
        ):
            return True
    return False


def default_leftover_roots() -> List[Path]:
    return [
        LOCALAPPDATA,
        APPDATA,
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")),
        USER / "Desktop",
        APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]


def filter_apps(apps: List[dict], query: str = "", limit: Optional[int] = None) -> List[dict]:
    q = query.strip().lower()
    out = []
    for app in apps:
        haystack = " ".join(str(app.get(k) or "") for k in ("name", "publisher", "version", "install_location")).lower()
        if q and q not in haystack:
            continue
        out.append(app)
        if limit is not None and len(out) >= limit:
            break
    return out


def find_registry_leftover_candidates(app: dict) -> List[str]:
    if os.name != "nt":
        return []
    # Match on the app NAME only — publisher substring matching caused every
    # product from a vendor (e.g. all "Microsoft" entries) to be flagged.
    tokens = normalize_app_tokens(app.get("name") or "")
    if not tokens:
        return []
    keys = []
    for row in list_installed_apps():
        key = str(row.get("uninstall_key") or "")
        rn_compact = "".join(ch.lower() for ch in str(row.get("name") or "") if ch.isalnum())
        if not rn_compact:
            continue
        if any(t == rn_compact or (len(t) >= 5 and rn_compact.startswith(t)) for t in tokens):
            keys.append(key)
    seen = set()
    out = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def find_leftover_candidates(app: dict, roots: Optional[List[Path]] = None) -> List[Item]:
    name = (app.get("name") or "").strip()
    publisher = (app.get("publisher") or "").strip()
    tokens = normalize_app_tokens(name, publisher)
    roots = roots or default_leftover_roots()
    out: List[Item] = []
    for root in roots:
        if not path_exists(root):
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if is_leftover_match(child, tokens) and not is_protected_path(child):
                try:
                    size = dir_size(child) if child.is_dir() else child.stat().st_size
                except OSError:
                    size = 0
                out.append(Item(path=child, size=size))
    out.sort(key=lambda item: item.size, reverse=True)
    return out


def cli_uninstall(json_out: bool, query: str = "", limit: Optional[int] = None, include_leftovers: bool = False) -> None:
    apps = list_installed_apps()
    apps = filter_apps(apps, query=query, limit=limit)
    if json_out:
        payload = {"apps": apps, "query": query, "limit": limit}
        if include_leftovers:
            payload["leftovers"] = [{
                "app": a.get("name"),
                "files": [{"path": str(i.path), "size": i.size} for i in find_leftover_candidates(a)[:30]],
                "registry_keys": find_registry_leftover_candidates(a),
            } for a in apps]
        print(json.dumps(payload, indent=2))
        return
    if not apps:
        print("No installed programs found.")
        return
    for i, app in enumerate(apps[:100], 1):
        ver = app.get("version") or ""
        pub = app.get("publisher") or ""
        print(f"{i:>3}. {app['name']} {ver} {pub}".strip())
    print("Use the TUI for launching uninstallers and inspecting install locations.")


def cli_status() -> None:
    if psutil is None:
        print("psutil not installed."); return
    print(f"Health: {health_score()}/100")
    print(f"CPU: {psutil.cpu_percent(interval=0.3)}%")
    vm = psutil.virtual_memory()
    print(f"Memory: {vm.percent}% ({human_size(vm.used)}/{human_size(vm.total)})")
    du = psutil.disk_usage(USER.anchor or 'C:\\')
    print(f"Disk: {du.percent}% ({human_size(du.free)} free)")


def cli_optimize(dry_run: bool, json_out: bool, yes: bool = False) -> None:
    rows = []
    for action in OPTIMIZE_ACTIONS:
        if action.risk == "high" and not dry_run:
            if json_out:
                # Non-interactive: never run destructive actions without --yes.
                if not yes:
                    rows.append({"title": action.title, "result": "skipped: high-risk requires --yes",
                                 "risk": action.risk, "admin": action.requires_admin})
                    continue
            elif not yes:
                print(f"High risk action '{action.title}'. Continue? [y/N] ", end="")
                if input().strip().lower() != "y":
                    rows.append({"title": action.title, "result": "cancelled"})
                    continue
        ok, opt_msg = run_optimize(action, dry_run=dry_run)
        rows.append({"title": action.title, "ok": ok, "result": opt_msg, "risk": action.risk, "admin": action.requires_admin})
    if json_out:
        print(json.dumps({"dry_run": dry_run, "results": rows}, indent=2))
    else:
        for row in rows:
            print(f"{row['title']}: {row['result']}")


def _parse_semver(v: str) -> tuple:
    """'v0.1.2' or '0.1.2' -> (0,1,2). Non-numeric chunks become -1."""
    parts = v.strip().lstrip("vV").split("-", 1)[0].split(".")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(-1)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def fetch_latest_release(repo: str = GITHUB_REPO, timeout: float = 10.0) -> Optional[dict]:
    """Return parsed GitHub latest-release JSON or None on failure."""
    import urllib.request
    import urllib.error
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"wmole/{__version__}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _pick_installer_asset(assets: List[dict]) -> Optional[dict]:
    if not assets:
        return None
    # Prefer Inno Setup installer by name
    for a in assets:
        if re.search(r"setup\.exe$", a.get("name", ""), re.IGNORECASE):
            return a
    # Fall back to any .exe or .msi
    for a in assets:
        if re.search(r"\.(exe|msi)$", a.get("name", ""), re.IGNORECASE):
            return a
    return assets[0]


def _is_installer_asset(name: str) -> bool:
    """True if the asset name looks like an Inno Setup / MSI installer (not a raw exe)."""
    return bool(re.search(r"(setup|installer|\.msi)$", name, re.IGNORECASE))


def _create_swap_batch(new_exe: Path, current_exe: Path) -> Path:
    """Create a .bat script that swaps new_exe → current_exe after this process exits.

    Returns the path to the batch file. The caller is responsible for launching it
    detached and then exiting.
    """
    batch = TEMP / "wmole-update.bat"
    # Batch script that:
    # 1. Waits for old wmole to fully exit (file unlocked)
    # 2. Copies new EXE over the old location
    # 3. Verifies the copy succeeded
    # 4. Starts the new EXE
    # 5. Self-deletes
    bat_content = (
        "@echo off\r\n"
        "setlocal enabledelayedexpansion\r\n"
        "title wmole update\r\n"
        'echo [wmole] Updating...\r\n'
        "\r\n"
        "rem Wait up to 15 seconds for the old process to release the file\r\n"
        "set /a attempts=0\r\n"
        ":waitloop\r\n"
        "   set /a attempts+=1\r\n"
        f'   copy /Y "{new_exe}" "{current_exe}" >nul 2>&1\r\n'
        "   if !errorlevel! equ 0 goto launch\r\n"
        "   if !attempts! geq 15 goto fail\r\n"
        "   timeout /t 1 /nobreak >nul 2>&1\r\n"
        "   goto waitloop\r\n"
        "\r\n"
        ":launch\r\n"
        f'   echo [wmole] Updated to {__version__}. Starting...\r\n'
        f'   start "" "{current_exe}"\r\n'
        "   del \"%~f0\" & exit /b 0\r\n"
        "\r\n"
        ":fail\r\n"
        '   echo [wmole] Update failed: could not replace old exe (file locked).\r\n'
        f'   echo [wmole] New version is at: {new_exe}\r\n'
        "   timeout /t 5 >nul\r\n"
        "   del \"%~f0\" & exit /b 1\r\n"
        "endlocal\r\n"
    )
    batch.write_text(bat_content, encoding="ascii")
    return batch


def _launch_updated(new_path: Path, asset_name: str, latest_tag: str) -> None:
    """Launch the downloaded update, handling both installers and raw EXEs.

    For Inno Setup / MSI installers: run with silent flags and exit.
    For raw EXEs: create a swap batch, launch it detached, and exit.
    """
    is_installer = _is_installer_asset(asset_name)
    DETACHED = 0x00000008  # DETACHED_PROCESS

    log_operation("auto_update", new_path, new_path.stat().st_size,
                  f"{__version__} -> {latest_tag}")

    if is_installer:
        # Inno Setup / MSI: run the installer silently
        subprocess.Popen(
            [str(new_path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            creationflags=DETACHED if os.name == "nt" else 0,
            close_fds=True,
        )
        try:
            PENDING_UPDATE_FILE.unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass
    else:
        # Raw EXE: create swap batch and launch it
        current_exe = Path(sys.executable) if getattr(sys, "frozen", False) else new_path
        batch = _create_swap_batch(new_path, current_exe)
        try:
            PENDING_UPDATE_FILE.unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass
        subprocess.Popen(
            ["cmd.exe", "/c", str(batch)],
            creationflags=DETACHED if os.name == "nt" else 0,
            close_fds=True,
        )

    sys.stderr.write(f"\n(wmole: installing update {latest_tag} in background)\n")
    os._exit(0)


def _download(url: str, dest: Path, on_progress=None) -> None:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": f"wmole/{__version__}"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            read += len(chunk)
            if on_progress and total:
                on_progress(read, total)


def cli_update(json_out: bool, yes: bool = False, dry_run: bool = False,
               quiet: bool = False, sink: Optional[dict] = None) -> str:
    """Self-update.

    - Frozen exe (PyInstaller install): check GitHub latest release, download
      installer, run /VERYSILENT to upgrade in place.
    - Source checkout: git pull + pip upgrade deps (legacy behavior).
    - quiet=True is used by the TUI to check status without writing behind its
      alternate screen or opening an invisible installation prompt.
    """
    is_frozen = bool(getattr(sys, "frozen", False))
    steps: List[dict] = []

    if is_frozen:
        rel = fetch_latest_release()
        if not rel:
            steps.append({"step": "fetch-release", "code": 1,
                          "output": "could not reach GitHub API"})
            return _emit_update(steps, json_out, quiet, sink)
        latest_tag = rel.get("tag_name", "")
        latest_ver = _parse_semver(latest_tag)
        current_ver = _parse_semver(__version__)
        steps.append({"step": "check-version", "code": 0,
                      "output": f"installed {__version__}, latest {latest_tag}"})
        if latest_ver <= current_ver:
            steps.append({"step": "decision", "code": 0,
                          "output": "already up to date"})
            return _emit_update(steps, json_out, quiet, sink)

        if quiet:
            steps.append({"step": "decision", "code": 0,
                          "output": f"update available: {latest_tag}; restart wmole to install automatically"})
            return _emit_update(steps, json_out, quiet, sink)

        asset = _pick_installer_asset(rel.get("assets", []))
        if not asset:
            steps.append({"step": "asset", "code": 1,
                          "output": "no installer asset found in release"})
            return _emit_update(steps, json_out, quiet, sink)
        url = asset["browser_download_url"]
        name = Path(asset["name"]).name  # strip any path components from the API value
        expected_size = int(asset.get("size") or 0)
        steps.append({"step": "asset", "code": 0, "output": name})

        if dry_run:
            steps.append({"step": "download", "code": 0,
                          "output": f"would download {url}"})
            steps.append({"step": "install", "code": 0,
                          "output": "would run /VERYSILENT /SUPPRESSMSGBOXES /NORESTART"})
            return _emit_update(steps, json_out, quiet, sink)

        if not yes and not json_out:
            try:
                ans = input(f"Upgrade wmole {__version__} -> {latest_tag} now? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                steps.append({"step": "decision", "code": 1, "output": "user declined"})
                return _emit_update(steps, json_out, quiet, sink)

        dest = TEMP / name
        try:
            last_pct = [-1]
            def _prog(read, total):
                pct = int(read * 100 / total)
                if pct != last_pct[0] and not json_out:
                    last_pct[0] = pct
                    sys.stdout.write(f"\rdownloading {name}: {pct:3d}%")
                    sys.stdout.flush()
            _download(url, dest, on_progress=_prog)
            if not json_out:
                sys.stdout.write("\n")
            if expected_size > 0 and dest.stat().st_size != expected_size:
                steps.append({"step": "download", "code": 1, "output": "size mismatch"})
                return _emit_update(steps, json_out, quiet, sink)
            steps.append({"step": "download", "code": 0,
                          "output": f"{dest} ({dest.stat().st_size} bytes)"})
        except Exception as exc:
            steps.append({"step": "download", "code": 1, "output": str(exc)})
            return _emit_update(steps, json_out, quiet, sink)

        # Launch installer detached so it can replace our own exe after we exit.
        try:
            DETACHED = 0x00000008  # DETACHED_PROCESS
            subprocess.Popen(
                [str(dest), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                creationflags=DETACHED if os.name == "nt" else 0,
                close_fds=True,
            )
            steps.append({"step": "install", "code": 0,
                          "output": "installer launched in background; exiting"})
            log_operation("self_update", dest, dest.stat().st_size,
                          f"{__version__} -> {latest_tag}")
            _emit_update(steps, json_out, quiet, sink)
            # Exit so the installer can overwrite the running exe.
            os._exit(0)
        except Exception as exc:
            steps.append({"step": "install", "code": 1, "output": str(exc)})
            return _emit_update(steps, json_out, quiet, sink)

    # Source checkout path (legacy)
    if path_exists(Path(".git")):
        r = subprocess.run(["git", "pull", "--ff-only"], capture_output=True, text=True, shell=False)
        steps.append({"step": "git-pull", "code": r.returncode, "output": (r.stdout or r.stderr).strip()})
    else:
        steps.append({"step": "git-pull", "code": 1, "output": "not a git repository"})
    r2 = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "rich", "send2trash", "psutil"], capture_output=True, text=True, shell=False)
    steps.append({"step": "pip-upgrade-deps", "code": r2.returncode, "output": (r2.stdout or r2.stderr).strip()[-500:]})
    return _emit_update(steps, json_out, quiet, sink)


# ---------- Background auto-update (Claude Code style) ----------
def _auto_update_disabled() -> bool:
    if os.environ.get("WMOLE_NO_AUTO_UPDATE", "").strip() not in ("", "0", "false", "False"):
        return True
    try:
        cfg = load_config()
        if cfg.get("auto_update") is False:
            return True
    except Exception:
        pass
    return False


def _auto_update_worker() -> None:
    """Background: throttle, check latest release, pre-download installer."""
    try:
        if _auto_update_disabled():
            return
        # Throttle: only hit GitHub once per AUTO_UPDATE_INTERVAL.
        last = 0.0
        if UPDATE_CHECK_FILE.exists():
            try:
                last = float(json.loads(UPDATE_CHECK_FILE.read_text(encoding="utf-8")).get("ts", 0))
            except Exception:
                last = 0.0
        if time.time() - last < AUTO_UPDATE_INTERVAL:
            return

        rel = fetch_latest_release(timeout=5.0)
        if not rel:
            return

        latest_tag = rel.get("tag_name", "")
        if _parse_semver(latest_tag) <= _parse_semver(__version__):
            # Up to date — clear any stale pending file.
            try:
                if PENDING_UPDATE_FILE.exists():
                    PENDING_UPDATE_FILE.unlink()
            except Exception:
                pass
            try:
                WMOLE_DIR.mkdir(parents=True, exist_ok=True)
                UPDATE_CHECK_FILE.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
            except Exception:
                pass
            return

        asset = _pick_installer_asset(rel.get("assets", []))
        if not asset:
            return
        expected_size = int(asset.get("size") or 0)
        dest = TEMP / Path(asset["name"]).name  # strip any path components

        # Skip download if we already have this exact installer staged.
        if PENDING_UPDATE_FILE.exists():
            try:
                cur = json.loads(PENDING_UPDATE_FILE.read_text(encoding="utf-8"))
                cur_path = Path(cur.get("path", ""))
                if (cur.get("version") == latest_tag and cur_path.exists()
                        and (expected_size == 0 or cur_path.stat().st_size == expected_size)):
                    return
            except Exception:
                pass

        if dest.exists() and expected_size and dest.stat().st_size == expected_size:
            pass  # already fully downloaded
        else:
            try:
                _download(asset["browser_download_url"], dest)
            except Exception:
                return

        try:
            PENDING_UPDATE_FILE.write_text(json.dumps({
                "version": latest_tag,
                "name": asset["name"],
                "path": str(dest),
                "downloaded_at": time.time(),
                "from_version": __version__,
            }), encoding="utf-8")
            try:
                WMOLE_DIR.mkdir(parents=True, exist_ok=True)
                UPDATE_CHECK_FILE.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        # Auto-update must never crash the host process.
        pass


def start_auto_update_check() -> None:
    """Kick off background update check. No-op outside frozen builds."""
    if not getattr(sys, "frozen", False):
        return
    if _auto_update_disabled():
        return
    try:
        t = threading.Thread(target=_auto_update_worker, name="wmole-autoupdate", daemon=True)
        t.start()
    except Exception:
        pass


def apply_pending_update() -> bool:
    """If a staged installer for a newer version exists, launch it detached.

    Returns True if an installer was launched (caller should exit promptly so
    the installer can replace the running exe).
    """
    if not getattr(sys, "frozen", False):
        return False
    if _auto_update_disabled():
        return False
    try:
        if not PENDING_UPDATE_FILE.exists():
            return False
        info = json.loads(PENDING_UPDATE_FILE.read_text(encoding="utf-8"))
        ver_tag = info.get("version", "")
        if _parse_semver(ver_tag) <= _parse_semver(__version__):
            PENDING_UPDATE_FILE.unlink(missing_ok=True)  # type: ignore[arg-type]
            return False
        path = Path(info.get("path", ""))
        asset_name = info.get("name", path.name)
        if not path.exists():
            PENDING_UPDATE_FILE.unlink(missing_ok=True)  # type: ignore[arg-type]
            return False
        # Smart launch: uses setup flags for installers, batch-swap for raw EXEs
        _launch_updated(path, asset_name, ver_tag)
        # _launch_updated calls os._exit(0), so we never reach here
        return True  # unreachable, but keeps type checker happy
    except Exception:
        return False


def _emit_update(steps: List[dict], json_out: bool, quiet: bool = False,
                 sink: Optional[dict] = None) -> str:
    # Yapılandırılmış sonucu doğrudan çağırana ver (stdout'a dokunmadan).
    # Sidecar bunu kullanır; böylece eşzamanlı işlemlerin stdout'una karışmaz.
    if sink is not None:
        sink["steps"] = steps
    if not quiet:
        if json_out:
            print(json.dumps({"steps": steps}, indent=2))
        else:
            for s in steps:
                print(f"{s['step']}: exit {s['code']}  {s['output']}")
    return steps[-1]["output"] if steps else ""


def cli_remove(dry_run: bool, json_out: bool, sink: Optional[dict] = None) -> None:
    targets = [WMOLE_DIR]
    results = []
    for t in targets:
        err = delete_path(t, use_trash=False, dry_run=dry_run)
        results.append({"path": str(t), "result": "ok" if err is None else err})
    if sink is not None:
        # Yapılandırılmış tüketici (sidecar) var: stdout'a hiçbir şey basma,
        # çünkü o kanal NDJSON event akışı.
        sink["dry_run"] = dry_run
        sink["results"] = results
        return
    if json_out:
        print(json.dumps({"dry_run": dry_run, "results": results}, indent=2))
    else:
        for row in results:
            print(f"{row['path']}: {row['result']}")


# ---------- Developer ports (listening localhost sockets) ----------
# Common dev-server defaults; used to flag entries as "well-known dev port".
DEV_PORT_HINTS: Dict[int, str] = {
    3000: "Node/React", 3001: "Node alt", 4000: "Phoenix/Gatsby",
    4200: "Angular", 5000: "Flask/.NET", 5173: "Vite", 5174: "Vite alt",
    5500: "Live Server", 8000: "Django/Python http", 8080: "Generic web",
    8081: "Webpack/Metro", 8888: "Jupyter", 9000: "PHP-FPM/SonarQube",
    9229: "Node debug", 27017: "MongoDB", 5432: "Postgres", 3306: "MySQL",
    6379: "Redis", 11434: "Ollama",
}

_LOCAL_IPS = {"127.0.0.1", "0.0.0.0", "::1", "::", ""}


def process_project_name(pid: Optional[int]) -> str:
    """Best-effort project label for a PID: git repo root name, else cwd name."""
    if not pid or not psutil:
        return ""
    try:
        cwd = psutil.Process(int(pid)).cwd()
    except (psutil.Error, OSError, ValueError):
        return ""
    if not cwd:
        return ""
    try:
        path = Path(cwd)
        for candidate in [path, *path.parents]:
            if (candidate / ".git").exists():
                return candidate.name
        return path.name
    except OSError:
        return ""


def list_dev_ports(include_all: bool = False) -> List[dict]:
    """Return listening TCP/UDP sockets bound to localhost/0.0.0.0.

    include_all=True keeps every listener regardless of bind address.
    """
    if not psutil:
        return []
    rows: Dict[tuple, dict] = {}
    projects: Dict[int, str] = {}
    for kind in ("tcp", "udp"):
        try:
            conns = psutil.net_connections(kind=kind)
        except (psutil.AccessDenied, PermissionError):
            # Need admin on Windows to enumerate other users' sockets.
            conns = []
        except Exception:
            conns = []
        for c in conns:
            if kind == "tcp" and c.status != psutil.CONN_LISTEN:
                continue
            if not c.laddr:
                continue
            ip = getattr(c.laddr, "ip", "")
            port = getattr(c.laddr, "port", 0)
            if not port:
                continue
            if not include_all and ip not in _LOCAL_IPS:
                continue
            key = (kind, ip, port, c.pid)
            if key in rows:
                continue
            name = ""
            exe = ""
            if c.pid:
                try:
                    proc = psutil.Process(c.pid)
                    name = proc.name()
                    try:
                        exe = proc.exe()
                    except (psutil.Error, OSError):
                        exe = ""
                except (psutil.Error, OSError):
                    pass
                if c.pid not in projects:
                    projects[c.pid] = process_project_name(c.pid)
            rows[key] = {
                "proto": kind.upper(),
                "ip": ip,
                "port": port,
                "pid": c.pid,
                "process": name,
                "exe": exe,
                "project": projects.get(c.pid, ""),
                "hint": DEV_PORT_HINTS.get(port, ""),
            }
    return sorted(rows.values(), key=lambda r: (r["port"], r["proto"]))


def process_protection_reason(pid: int) -> str:
    if not psutil:
        return ""
    try:
        proc = psutil.Process(pid)
        name = (proc.name() or "").lower()
        protected = {str(v).lower() for v in load_config().get("protected_processes", [])}
        if name in protected:
            return f"protected developer process: {name}"
        try:
            cwd = Path(proc.cwd())
            repo = _git_repository(cwd)
            if repo:
                result = subprocess.run(
                    ["git", "-C", str(repo), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=10, shell=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return f"process works in a repository with unsaved changes: {repo}"
        except (psutil.AccessDenied, OSError, subprocess.SubprocessError):
            pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return ""


def kill_pid(pid: int, dry_run: bool = False, force: bool = True) -> tuple:
    """Returns (ok: bool, message: str)."""
    if not pid:
        return (False, "no pid")
    if dry_run:
        return (True, f"would kill pid {pid}")
    if not psutil:
        return (False, "psutil unavailable")
    protection = process_protection_reason(pid)
    if protection:
        log_security_event("process-kill-blocked", {"pid": pid, "reason": protection})
        return (False, protection)
    try:
        proc = psutil.Process(pid)
        if force:
            proc.kill()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except (psutil.Error, OSError):
            pass
        return (True, f"killed pid {pid}")
    except psutil.NoSuchProcess:
        return (True, f"pid {pid} already gone")
    except psutil.AccessDenied:
        return (False, f"access denied for pid {pid} (run as admin)")
    except Exception as exc:
        return (False, f"failed pid {pid}: {exc}")


def cli_ports(json_out: bool, kill_target: str = "", dry_run: bool = False,
              include_all: bool = False) -> None:
    """List listening dev ports, or kill by port/PID/'all'."""
    rows = list_dev_ports(include_all=include_all)

    if kill_target:
        target = kill_target.strip().lower()
        if target == "all":
            victims = rows
        elif target.startswith("port:") and target[5:].isdigit():
            n = int(target[5:])
            victims = [r for r in rows if r["port"] == n]
        elif target.startswith("pid:") and target[4:].isdigit():
            n = int(target[4:])
            victims = [r for r in rows if r["pid"] == n]
        elif target.isdigit():
            n = int(target)
            # Bare number is ambiguous: prefer port, fall back to PID.
            # Use 'port:N' or 'pid:N' to be explicit.
            victims = [r for r in rows if r["port"] == n] or \
                      [r for r in rows if r["pid"] == n]
        else:
            print(json.dumps({"error": f"invalid --kill target: {kill_target}"}, indent=2)
                  if json_out else f"invalid --kill target: {kill_target} (use <port>, port:N, pid:N, or all)")
            return
        results = []
        seen_pids = set()
        for r in victims:
            if not r["pid"] or r["pid"] in seen_pids:
                continue
            seen_pids.add(r["pid"])
            k_ok, msg = kill_pid(r["pid"], dry_run=dry_run)
            log_operation("port_kill", Path(f"pid:{r['pid']}:{r['proto']}:{r['port']}"),
                          0, msg)
            results.append({**r, "ok": k_ok, "result": msg})
        if json_out:
            print(json.dumps({"killed": results, "dry_run": dry_run}, indent=2))
        else:
            if not results:
                print("no matching listening ports.")
                return
            for r in results:
                print(f"{r['proto']}/{r['port']:>5}  pid {r['pid']:>6}  "
                      f"{r['process'] or '-':<20}  {r['result']}")
        return

    if json_out:
        print(json.dumps({"ports": rows, "count": len(rows)}, indent=2))
        return

    if not rows:
        print("no listening localhost ports detected (run as admin to see all).")
        return
    print(f"{'PROTO':<5} {'PORT':>6}  {'PID':>6}  {'PROCESS':<22} {'PROJECT':<20} {'BIND':<16} HINT")
    print("-" * 98)
    for r in rows:
        print(f"{r['proto']:<5} {r['port']:>6}  {str(r['pid'] or '-'):>6}  "
              f"{(r['process'] or '-')[:22]:<22} {(r.get('project') or '-')[:20]:<20} "
              f"{r['ip']:<16} {r['hint']}")
    print()
    print("Kill: wmole ports --kill <port|port:N|pid:N|all>   (add --dry-run to preview)")


DOCTOR_TOOLS = ("python", "node", "npm", "git")
DOCTOR_CACHE_KEYS = ("npm-cache", "pip-cache", "yarn-cache", "cargo-cache")
DOCTOR_CACHE_WARN_BYTES = 2 * 1024**3


def _doctor_row(check: str, status: str, detail: str) -> dict:
    return {"check": check, "status": status, "detail": detail}


def _doctor_path_entries() -> List[dict]:
    """PATH hygiene: duplicated and non-existent entries."""
    raw = [part.strip().strip('"') for part in os.environ.get("PATH", "").split(os.pathsep)]
    entries = [part for part in raw if part]
    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    missing: List[str] = []
    for entry in entries:
        key = os.path.normcase(os.path.expandvars(entry)).rstrip("\\/")
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            duplicates.append(entry)
        try:
            if not Path(os.path.expandvars(entry)).exists():
                missing.append(entry)
        except OSError:
            missing.append(entry)
    rows = [_doctor_row("PATH entries", "ok", f"{len(entries)} entry(s)")]
    rows.append(_doctor_row(
        "PATH duplicates", "warn" if duplicates else "ok",
        ", ".join(duplicates[:5]) if duplicates else "no duplicates"))
    rows.append(_doctor_row(
        "PATH missing", "warn" if missing else "ok",
        ", ".join(missing[:5]) if missing else "all entries exist"))
    return rows


def _tool_version(name: str, timeout: int = 8) -> dict:
    executable = shutil.which(name)
    if not executable:
        return _doctor_row(name, "unavailable", "not found on PATH")
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                timeout=timeout, shell=False)
    except (subprocess.SubprocessError, OSError) as exc:
        return _doctor_row(name, "fail", f"version check failed: {exc}")
    output = (result.stdout or result.stderr or "").strip().splitlines()
    version = output[0].strip() if output else ""
    if result.returncode != 0 or not version:
        return _doctor_row(name, "fail", f"unexpected output from {executable}")
    return _doctor_row(name, "ok", f"{version} ({executable})")


def _doctor_caches() -> List[dict]:
    rows: List[dict] = []
    by_key = {entry[0]: entry for entry in FIXED_PATH_CATEGORIES}
    for key in DOCTOR_CACHE_KEYS:
        entry = by_key.get(key)
        if not entry:
            continue
        title, path = entry[1], entry[3]
        if not path_exists(path):
            rows.append(_doctor_row(title, "unavailable", "not present"))
            continue
        try:
            size = quick_size(path)
        except OSError:
            rows.append(_doctor_row(title, "unavailable", "unreadable"))
            continue
        status = "warn" if size >= DOCTOR_CACHE_WARN_BYTES else "ok"
        rows.append(_doctor_row(title, status, f"{human_size(size)} at {path}"))
    return rows


def _doctor_disk() -> dict:
    root = USER.anchor or "C:\\"
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        return _doctor_row("system disk", "unavailable", f"{root}: {exc}")
    free_ratio = usage.free / usage.total if usage.total else 0.0
    if free_ratio < 0.05:
        status = "fail"
    elif free_ratio < 0.15:
        status = "warn"
    else:
        status = "ok"
    return _doctor_row("system disk", status,
                       f"{human_size(usage.free)} free of {human_size(usage.total)} on {root}")


def _doctor_wmole_state() -> List[dict]:
    rows: List[dict] = []
    if not path_exists(WMOLE_DIR):
        return [_doctor_row("~/.wmole", "unavailable", "not initialized yet")]
    if path_exists(CONFIG_FILE):
        try:
            json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            rows.append(_doctor_row("config.json", "ok", str(CONFIG_FILE)))
        except (OSError, ValueError) as exc:
            rows.append(_doctor_row("config.json", "fail", f"unreadable: {exc}"))
    else:
        rows.append(_doctor_row("config.json", "unavailable", "using built-in defaults"))
    if path_exists(CACHE_FILE):
        try:
            json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            rows.append(_doctor_row("cache.json", "ok", str(CACHE_FILE)))
        except (OSError, ValueError) as exc:
            rows.append(_doctor_row("cache.json", "warn", f"corrupt, delete to reset: {exc}"))
    else:
        rows.append(_doctor_row("cache.json", "unavailable", "no size cache yet"))
    entries = _quarantine_entries()
    try:
        size = quick_size(QUARANTINE_DIR) if path_exists(QUARANTINE_DIR) else 0
    except OSError:
        size = 0
    status = "warn" if size >= 1024**3 else "ok"
    rows.append(_doctor_row("quarantine", status,
                            f"{len(entries)} entry(s), {human_size(size)}"))
    return rows


def doctor_report() -> dict:
    """Developer-machine health snapshot as a list of {check,status,detail} rows."""
    checks: List[dict] = []
    checks.extend(_doctor_path_entries())
    for tool in DOCTOR_TOOLS:
        checks.append(_tool_version(tool))
    checks.extend(_doctor_caches())
    checks.append(_doctor_disk())
    checks.extend(_doctor_wmole_state())
    summary = {"ok": 0, "warn": 0, "fail": 0, "unavailable": 0}
    for row in checks:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    return {"checks": checks, "summary": summary,
            "healthy": summary.get("fail", 0) == 0}


DOCTOR_STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red", "unavailable": "grey50"}
DOCTOR_STATUS_ICON = {"ok": "+", "warn": "!", "fail": "x", "unavailable": "-"}


def cli_doctor(json_out: bool = False) -> None:
    report = doctor_report()
    if json_out:
        print(json.dumps(report, indent=2))
        return
    console.print("[bold]wmole doctor[/bold]")
    for row in report["checks"]:
        style = DOCTOR_STATUS_STYLE.get(row["status"], "white")
        icon = DOCTOR_STATUS_ICON.get(row["status"], "?")
        console.print(f"  [{style}]{icon}[/{style}] {row['check']:<26} "
                      f"[{style}]{row['status']:<12}[/{style}] {row['detail']}")
    s = report["summary"]
    console.print(f"\n  ok={s.get('ok', 0)}  warn={s.get('warn', 0)}  "
                  f"fail={s.get('fail', 0)}  unavailable={s.get('unavailable', 0)}")


def undo_entries(limit: int = 20) -> List[dict]:
    """Most recent quarantine entries, newest first."""
    entries = sorted(_quarantine_entries(), key=lambda row: int(row.get("created", 0)),
                     reverse=True)
    rows = []
    for entry in entries[:limit]:
        created = int(entry.get("created", 0))
        rows.append({
            "id": entry.get("id", ""),
            "path": entry.get("original", ""),
            "name": entry.get("name", ""),
            "size": int(entry.get("size", 0) or 0),
            "created": created,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created)) if created else "",
        })
    return rows


RECYCLE_BIN_HINT = ("Items deleted straight to the Recycle Bin are not tracked here - "
                    "restore those from the Windows Recycle Bin.")


def cli_undo(entry_id: str = "", json_out: bool = False, limit: int = 20) -> None:
    """List restorable quarantine entries, or restore one by id."""
    if entry_id:
        error = restore_quarantine(entry_id)
        payload = {"id": entry_id, "restored": error is None, "error": error}
        if json_out:
            print(json.dumps(payload, indent=2))
        elif error:
            console.print(f"[red]undo failed:[/red] {error}")
            console.print(f"[grey50]{RECYCLE_BIN_HINT}[/grey50]")
        else:
            console.print(f"[green]restored[/green] {entry_id}")
        return
    rows = undo_entries(limit=limit)
    if json_out:
        print(json.dumps({"entries": rows, "count": len(rows),
                          "hint": RECYCLE_BIN_HINT}, indent=2))
        return
    if not rows:
        console.print("no quarantined items to undo.")
        console.print(f"[grey50]{RECYCLE_BIN_HINT}[/grey50]")
        return
    console.print(f"{'ID':<24} {'WHEN':<20} {'SIZE':>10}  PATH")
    for row in rows:
        console.print(f"{row['id']:<24} {row['created_at']:<20} "
                      f"{human_size(row['size']):>10}  {row['path']}")
    console.print("\nRestore: wmole undo <id>")
    console.print(f"[grey50]{RECYCLE_BIN_HINT}[/grey50]")


HOOK_SIGNATURE = "# wmole-pre-commit-hook"
HOOK_TEMPLATE = """#!/bin/sh
{signature}
# Blocks commits containing secrets. Remove with: wmole hook uninstall
{command} hook --run || exit 1
exit 0
"""


def _hook_command() -> str:
    """Kurulu hook'un çağıracağı komut.

    Frozen build'de sys.executable zaten wmole.exe'dir; ayrıca `__file__`
    PyInstaller'ın geçici çıkarma dizinini gösterir ve süreç bitince silinir,
    yani onu hook'a yazmak kalıcı olarak bozuk bir hook üretir.
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{Path(sys.executable).resolve()}" "{Path(__file__).resolve()}"'


def git_repo_root(start: Path) -> Optional[Path]:
    """Nearest ancestor containing a `.git` directory."""
    try:
        base = start if start.is_dir() else start.parent
    except OSError:
        base = start
    for candidate in [base, *base.parents]:
        if (candidate / ".git").is_dir():
            return candidate
    return None


def _staged_files(root: Path) -> List[Path]:
    git = shutil.which("git")
    if not git:
        return []
    try:
        result = subprocess.run([git, "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                                cwd=str(root), capture_output=True, text=True,
                                timeout=30, shell=False)
    except (subprocess.SubprocessError, OSError) as exc:
        _debug_log(f"hook: staged file listing failed: {exc}")
        return []
    if result.returncode != 0:
        return []
    out = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line:
            out.append(root / line)
    return out


def scan_staged_secrets(root: Path) -> dict:
    """Run the secret patterns over staged files only."""
    findings: list[dict] = []
    files = _staged_files(root)
    for path in files:
        try:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for kind, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    value = match.group(0)
                    preview = value[:4] + "..." + value[-4:] if len(value) > 10 else "[redacted]"
                    findings.append({"type": kind, "path": str(path), "line": line_no,
                                     "preview": preview, "severity": "high"})
                    break
    return {"root": str(root), "scanned": len(files), "findings": findings}


def hook_path(root: Path) -> Path:
    return root / ".git" / "hooks" / "pre-commit"


def install_pre_commit_hook(root: Path, force: bool = False) -> dict:
    target = hook_path(root)
    if path_exists(target):
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "path": str(target), "error": f"unreadable hook: {exc}"}
        if HOOK_SIGNATURE not in existing and not force:
            return {"ok": False, "path": str(target),
                    "error": "a pre-commit hook already exists; re-run with --force to replace it"}
    body = HOOK_TEMPLATE.format(signature=HOOK_SIGNATURE, command=_hook_command())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
        os.chmod(target, 0o755)
    except OSError as exc:
        return {"ok": False, "path": str(target), "error": f"write failed: {exc}"}
    log_security_event("hook-installed", {"path": str(target)})
    return {"ok": True, "path": str(target), "action": "installed"}


def uninstall_pre_commit_hook(root: Path) -> dict:
    target = hook_path(root)
    if not path_exists(target):
        return {"ok": True, "path": str(target), "action": "absent"}
    try:
        existing = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "path": str(target), "error": f"unreadable hook: {exc}"}
    if HOOK_SIGNATURE not in existing:
        return {"ok": False, "path": str(target),
                "error": "pre-commit hook was not installed by wmole; left untouched"}
    try:
        target.unlink()
    except OSError as exc:
        return {"ok": False, "path": str(target), "error": f"remove failed: {exc}"}
    log_security_event("hook-uninstalled", {"path": str(target)})
    return {"ok": True, "path": str(target), "action": "removed"}


def cli_hook(action: str, json_out: bool = False, force: bool = False,
             cwd: Optional[Path] = None) -> int:
    """`wmole hook install|uninstall|--run` for the git repo at `cwd`."""
    base = cwd or Path.cwd()
    root = git_repo_root(base)
    if not root:
        payload = {"ok": False, "error": "not inside a git repository"}
        print(json.dumps(payload, indent=2) if json_out else payload["error"])
        return 1
    if action == "run":
        report = scan_staged_secrets(root)
        if json_out:
            print(json.dumps(report, indent=2))
        elif report["findings"]:
            console.print("[red]wmole: potential secrets in staged files[/red]")
            for row in report["findings"]:
                console.print(f"  {row['type']:<16} {row['path']}:{row['line']}  {row['preview']}")
            console.print("Commit blocked. Remove the secrets or use `git commit --no-verify`.")
        return 1 if report["findings"] else 0
    if action == "install":
        result = install_pre_commit_hook(root, force=force)
    elif action == "uninstall":
        result = uninstall_pre_commit_hook(root)
    else:
        result = {"ok": False, "error": f"unknown hook action: {action} (use install/uninstall)"}
    if json_out:
        print(json.dumps(result, indent=2))
    elif result.get("ok"):
        console.print(f"[green]hook {result.get('action', 'ok')}[/green] {result.get('path', '')}")
    else:
        console.print(f"[red]{result.get('error', 'failed')}[/red]")
    return 0 if result.get("ok") else 1


def powershell_completion_script() -> str:
    return """Register-ArgumentCompleter -CommandName wmole, py -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $modes = 'analyze','clean','purge','status','optimize','uninstall','installer','installers','update','remove','completion','ports','doctor','undo','hook'
    $modes | Where-Object { $_ -like \"$wordToComplete*\" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}"""


def cli_completion(shell: str, install: bool, json_out: bool) -> None:
    if shell.lower() != "powershell":
        msg = "only powershell is supported currently"
        print(json.dumps({"error": msg}, indent=2) if json_out else msg)
        return
    script = powershell_completion_script()
    if install:
        WMOLE_DIR.mkdir(parents=True, exist_ok=True)
        COMPLETION_FILE.write_text(script + "\n", encoding="utf-8")
    if json_out:
        print(json.dumps({"shell": shell, "installed": install, "path": str(COMPLETION_FILE), "script": script}, indent=2))
    else:
        print(script)


# ---------- Benchmark harness ----------
# Throughput claims are only worth making if anyone can reproduce them, so the
# harness ships with the tool: `wmole bench --json`. It builds a synthetic
# tree, times the scan and the delete separately, and reports items/sec along
# with enough machine detail to make the number interpretable.

BENCH_DEFAULT_DIRS = 200
BENCH_DEFAULT_FILES_PER_DIR = 50
BENCH_DEFAULT_FILE_BYTES = 4096


def build_bench_tree(root: Path, dirs: int, files_per_dir: int,
                     file_bytes: int) -> int:
    """Materialise a synthetic tree; returns the number of files created."""
    payload = b"w" * file_bytes
    created = 0
    for d in range(dirs):
        # Two levels, so the walk pays for real directory recursion.
        sub = root / f"pkg{d // 20:03d}" / f"mod{d:04d}"
        sub.mkdir(parents=True, exist_ok=True)
        for f in range(files_per_dir):
            (sub / f"file{f:03d}.bin").write_bytes(payload)
            created += 1
    return created


def _bench_machine() -> dict:
    info = {"platform": platform.platform(), "python": platform.python_version(),
            "cpu_count": os.cpu_count() or 0}
    if psutil is not None:
        try:
            info["cpu_freq_mhz"] = round(psutil.cpu_freq().current)
        except Exception:
            pass
        try:
            info["ram_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
        except Exception:
            pass
    return info


def run_benchmark(dirs: int = BENCH_DEFAULT_DIRS,
                  files_per_dir: int = BENCH_DEFAULT_FILES_PER_DIR,
                  file_bytes: int = BENCH_DEFAULT_FILE_BYTES,
                  workers: int = 0, keep: bool = False) -> dict:
    """Measure scan and delete throughput on a freshly built tree.

    The tree is built under %TEMP% and removed afterwards. Build time is
    reported but excluded from both throughput figures.
    """
    workers = workers or min(32, (os.cpu_count() or 4) * 4)
    base = Path(TEMP) / f"wmole-bench-{secrets.token_hex(4)}"
    base.mkdir(parents=True, exist_ok=True)
    result: dict = {"machine": _bench_machine(), "version": __version__,
                    "params": {"dirs": dirs, "files_per_dir": files_per_dir,
                               "file_bytes": file_bytes, "workers": workers}}
    try:
        t0 = time.perf_counter()
        files = build_bench_tree(base, dirs, files_per_dir, file_bytes)
        result["build_seconds"] = round(time.perf_counter() - t0, 3)
        result["files"] = files

        # --- scan: walk every entry and compute both size views ---
        t0 = time.perf_counter()
        facts = disk_facts(base)
        scan_seconds = time.perf_counter() - t0
        result["scan"] = {
            "seconds": round(scan_seconds, 3),
            "items": facts.files,
            "items_per_second": round(facts.files / scan_seconds) if scan_seconds else 0,
            "logical_bytes": facts.logical,
            "on_disk_bytes": facts.on_disk,
        }

        # --- parallel sizing: the path a real scan takes ---
        leaves = [p for p in base.rglob("mod*") if p.is_dir()]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            sized = sum(1 for _ in pool.map(lambda p: sized_dir(p, cache=None), leaves))
        parallel_seconds = time.perf_counter() - t0
        result["parallel_sizing"] = {
            "seconds": round(parallel_seconds, 3),
            "dirs": sized,
            "dirs_per_second": round(sized / parallel_seconds) if parallel_seconds else 0,
        }

        # --- delete: handle-addressed removal of the whole tree ---
        subtrees = sorted(p for p in base.iterdir() if p.is_dir())
        t0 = time.perf_counter()
        errors = []
        for sub in subtrees:
            err = permanent_delete(sub)
            if err:
                errors.append({"path": str(sub), "error": err})
        delete_seconds = time.perf_counter() - t0
        result["delete"] = {
            "seconds": round(delete_seconds, 3),
            "items": files,
            "items_per_second": round(files / delete_seconds) if delete_seconds else 0,
            "handle_based": handle_delete_supported(),
            "errors": errors,
        }
        return result
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            result["kept_at"] = str(base)


def cli_bench(json_out: bool, dirs: int, files_per_dir: int, file_bytes: int,
              keep: bool) -> None:
    report = run_benchmark(dirs=dirs, files_per_dir=files_per_dir,
                           file_bytes=file_bytes, keep=keep)
    if json_out:
        print(json.dumps(report, indent=2))
        return
    console = Console()
    console.print(f"[bold]wmole bench[/bold]  v{report['version']}  "
                  f"{report['machine'].get('platform', '')}")
    console.print(f"  tree      {report['files']} files in "
                  f"{report['params']['dirs']} dirs "
                  f"(built in {report['build_seconds']}s)")
    scan = report["scan"]
    console.print(f"  scan      {scan['items_per_second']} items/s "
                  f"({scan['items']} in {scan['seconds']}s)")
    par = report["parallel_sizing"]
    console.print(f"  sizing    {par['dirs_per_second']} dirs/s "
                  f"({par['dirs']} in {par['seconds']}s, "
                  f"{report['params']['workers']} workers)")
    dele = report["delete"]
    mode = "handle-based" if dele["handle_based"] else "name-based"
    console.print(f"  delete    {dele['items_per_second']} items/s "
                  f"({dele['items']} in {dele['seconds']}s, {mode})")
    if dele["errors"]:
        console.print(f"  [red]{len(dele['errors'])} delete error(s)[/red]")


def cli_explain(paths: List[Path], json_out: bool) -> None:
    """Print the explanation card and risk score for each path."""
    rows = []
    for p in paths:
        card = explain_item(p)
        risk = risk_score(p)
        rows.append({"path": str(p), "explain": card.as_dict(), "risk": risk.as_dict()})
    if json_out:
        print(json.dumps(rows, indent=2))
        return
    console = Console()
    for row in rows:
        card, risk = row["explain"], row["risk"]
        console.print(f"\n[bold]{row['path']}[/bold]")
        console.print(f"  what        {card['what']}")
        console.print(f"  you lose    {card['lost']}")
        console.print(f"  you keep    {card['kept']}")
        console.print(f"  rebuild     {card['rebuild'] or '— nothing recreates this —'}"
                      "   [dim](shown for reference; wmole never runs it)[/dim]")
        console.print(f"  space back  {card['when_freed']}")
        console.print(f"  reversible  {card['reversible']}")
        colour = {"low": "green", "moderate": "yellow",
                  "high": "red", "critical": "bold red"}[risk["level"]]
        console.print(f"  risk        [{colour}]{risk['score']}/100 ({risk['level']})[/{colour}]")
        for mod in risk["modifiers"]:
            sign = "+" if mod["points"] >= 0 else ""
            console.print(f"                {sign}{mod['points']:>4}  {mod['label']}")


def main_cli() -> None:
    p = argparse.ArgumentParser(prog="wmole", description="Windows port of mole")
    p.add_argument("mode", nargs="?", default="analyze",
                   choices=["analyze", "clean", "purge", "status", "optimize", "uninstall", "installer", "installers", "update", "remove", "completion", "ports", "doctor", "undo", "hook", "serve", "bench", "explain", "verify"])
    p.add_argument("targets", nargs="*", help="optional paths for analyze/purge/installers")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true", dest="json_out")
    p.add_argument("--query", default="", help="filter uninstall entries")
    p.add_argument("--limit", type=int, default=None, help="limit uninstall JSON/list output")
    p.add_argument("--leftovers", action="store_true", help="include leftover candidates in uninstall JSON")
    p.add_argument("--paths", default="", help="semicolon/comma separated paths override")
    p.add_argument("--whitelist", default=str(WHITELIST_FILE), help="whitelist file path")
    p.add_argument("--shell", default="powershell", help="completion shell")
    p.add_argument("--install", action="store_true", help="install completion script under ~/.wmole")
    p.add_argument("--yes", "-y", action="store_true", help="auto-confirm prompts (e.g. self-update)")
    p.add_argument("--disable-auto", action="store_true", help="update mode: turn off background auto-update")
    p.add_argument("--enable-auto", action="store_true", help="update mode: turn background auto-update back on")
    p.add_argument("--kill", default="", help="ports mode: <port>, <pid>, or 'all'")
    p.add_argument("--all-binds", action="store_true", help="ports mode: include non-localhost listeners")
    p.add_argument("--no-cache", action="store_true", help="bypass size cache for this run")
    p.add_argument("--force", action="store_true", help="hook mode: overwrite an existing pre-commit hook")
    p.add_argument("--run", action="store_true", help="hook mode: scan staged files (used by the installed hook)")
    p.add_argument("--dirs", type=int, default=BENCH_DEFAULT_DIRS, help="bench mode: directories to create")
    p.add_argument("--files-per-dir", type=int, default=BENCH_DEFAULT_FILES_PER_DIR, help="bench mode: files per directory")
    p.add_argument("--file-bytes", type=int, default=BENCH_DEFAULT_FILE_BYTES, help="bench mode: bytes per file")
    p.add_argument("--keep", action="store_true", help="bench mode: leave the generated tree in place")
    args = p.parse_args()

    # Do not start a final background check while the user is disabling it.
    # Other commands preserve the fire-and-forget startup behavior.
    if args.mode not in ("serve", "hook") and not (args.mode == "update" and args.disable_auto):
        start_auto_update_check()

    target_paths = [Path(t).expanduser() for t in args.targets]
    override_paths = parse_paths_arg(args.paths)
    if override_paths:
        target_paths = override_paths
    whitelist = load_whitelist(Path(args.whitelist).expanduser())

    if args.mode == "analyze" and args.json_out:
        cli_analyze(target_paths[0] if target_paths else USER, json_out=True)
    elif args.mode == "analyze" and target_paths:
        run_tui(initial_view="analyze", start_path=target_paths[0], use_cache=not args.no_cache)
    elif args.mode == "clean":
        cli_clean(dry_run=args.dry_run, json_out=args.json_out, roots=target_paths or None, whitelist=whitelist, use_cache=not args.no_cache)
    elif args.mode == "purge":
        roots = target_paths or load_purge_roots() or discover_scan_roots()
        cli_categories(build_purge_categories(roots, whitelist=whitelist),
                       dry_run=args.dry_run, json_out=args.json_out, label="purge")
    elif args.mode in ("installer", "installers"):
        roots = target_paths or [USER / "Downloads", USER / "Desktop"]
        cli_categories(build_installer_categories(roots),
                       dry_run=args.dry_run, json_out=args.json_out, label="installers")
    elif args.mode == "serve":
        cmd_serve()
    elif args.mode == "status" and args.json_out:
        print(json.dumps(status_payload(), indent=2))
    elif args.mode == "status":
        cli_status()
    elif args.mode == "optimize":
        cli_optimize(dry_run=args.dry_run, json_out=args.json_out, yes=args.yes)
    elif args.mode == "uninstall" and args.json_out:
        cli_uninstall(json_out=True, query=args.query, limit=args.limit, include_leftovers=args.leftovers)
    elif args.mode == "update":
        if args.disable_auto or args.enable_auto:
            try:
                cfg = load_config()
                cfg["auto_update"] = not args.disable_auto
                WMOLE_DIR.mkdir(parents=True, exist_ok=True)
                CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                state = "enabled" if cfg["auto_update"] else "disabled"
                msg = f"background auto-update {state}"
                print(json.dumps({"auto_update": cfg["auto_update"]}, indent=2)
                      if args.json_out else msg)
            except Exception as exc:
                print(f"failed to update config: {exc}")
            return
        cli_update(json_out=args.json_out, yes=args.yes, dry_run=args.dry_run)
    elif args.mode == "remove":
        cli_remove(dry_run=args.dry_run, json_out=args.json_out)
    elif args.mode == "completion":
        cli_completion(shell=args.shell, install=args.install, json_out=args.json_out)
    elif args.mode == "ports":
        cli_ports(json_out=args.json_out, kill_target=args.kill,
                  dry_run=args.dry_run, include_all=args.all_binds)
    elif args.mode == "doctor":
        cli_doctor(json_out=args.json_out)
    elif args.mode == "undo":
        cli_undo(entry_id=args.targets[0] if args.targets else "", json_out=args.json_out)
    elif args.mode == "bench":
        cli_bench(json_out=args.json_out, dirs=args.dirs,
                  files_per_dir=args.files_per_dir, file_bytes=args.file_bytes,
                  keep=args.keep)
    elif args.mode == "explain":
        cli_explain(target_paths or [USER], json_out=args.json_out)
    elif args.mode == "verify":
        outcomes = JOURNAL.recover()
        entries = []
        for entry in _quarantine_entries():
            stored = Path(entry.get("stored", ""))
            problem = ("quarantined content is missing" if not path_exists(stored)
                       else verify_digest(stored, entry.get("digest", ""),
                                          entry.get("digest_algo", "")))
            entries.append({"id": entry.get("id"), "original": entry.get("original"),
                            "ok": problem is None, "problem": problem or ""})
        if args.json_out:
            print(json.dumps({"recovered": outcomes, "quarantine": entries}, indent=2))
        else:
            console = Console()
            for row in outcomes:
                console.print(f"[yellow]recovered[/yellow] {row['verdict']}  {row['path']}"
                              f"\n            {row['detail']}")
            for row in entries:
                mark = "[green]ok[/green]" if row["ok"] else "[red]FAILED[/red]"
                console.print(f"{mark}  {row['original']}"
                              + (f"\n     {row['problem']}" if row["problem"] else ""))
            if not outcomes and not entries:
                console.print("nothing in quarantine, journal clean")
    elif args.mode == "hook":
        action = "run" if args.run else (args.targets[0].strip().lower() if args.targets else "")
        sys.exit(cli_hook(action=action, json_out=args.json_out, force=args.force))
    else:
        run_tui(initial_view=args.mode if args.mode in ("status", "optimize", "uninstall", "purge", "installer", "installers") else "analyze", use_cache=not args.no_cache)


if __name__ == "__main__":
    try:
        main_cli()
    except KeyboardInterrupt:
        pass
    finally:
        # If a newer installer is staged, fire it detached on the way out so it
        # can replace this exe. Silent no-op in dev/source mode.
        try:
            apply_pending_update()
        except Exception:
            pass
