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
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
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
__version__ = "0.3.8"
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


def dir_size(path: Path, on_progress=None, max_seconds: Optional[float] = None,
             max_files: Optional[int] = None) -> int:
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
        walk(path)
    except Exception:
        pass
    if on_progress:
        on_progress(total)
    return total


def load_size_cache() -> dict:
    """Returns {path_str: {"mtime": float, "size": int, "scanned_at": float}}."""
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_size_cache(cache: dict) -> None:
    try:
        WMOLE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except Exception:
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


def sized_dir(path: Path, cache: Optional[dict] = None,
              lock: "Optional[threading.Lock]" = None) -> int:
    """Compute dir size, consulting/updating an optional mtime cache (in place)."""
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
            return hit
    size = dir_size(path)
    if cache is not None and mtime is not None:
        if lock is not None:
            with lock:
                cache_set(cache, path, mtime, size)
        else:
            cache_set(cache, path, mtime, size)
    return size


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
    return "tr"


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
        except Exception:
            pass
        return defaults
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            merged = defaults.copy()
            merged.update(loaded)
            return merged
    except Exception:
        pass
    return defaults


def load_path_list(path_file: Path) -> List[Path]:
    out: List[Path] = []
    if not path_exists(path_file):
        return out
    try:
        for line in path_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(Path(os.path.expandvars(os.path.expanduser(line))))
    except Exception:
        pass
    return out


def load_whitelist(path_file: Path = WHITELIST_FILE) -> List[Path]:
    return load_path_list(path_file)


def load_denylist(path_file: Path = DENYLIST_FILE) -> List[Path]:
    return load_path_list(path_file)


def is_whitelisted(path: Path, wl: List[Path]) -> bool:
    p = path.resolve() if path.exists() else path
    for w in wl:
        try:
            if p == w or w in p.parents:
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
    except Exception:
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
    except Exception:
        pass


def _onerr_chmod(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
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
    try:
        if path.is_file() or path.is_symlink() or _is_reparse_point(path):
            # Remove the link/junction itself; never recurse into its target.
            try:
                path.unlink()
            except (IsADirectoryError, PermissionError, OSError):
                os.rmdir(path)
        else:
            shutil.rmtree(path, onerror=_onerr_chmod)
        log_operation("delete-permanent", path, size=size, result="ok")
        return None
    except Exception as e:
        log_operation("delete-permanent", path, size=size, result=str(e))
        return str(e)


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
             "Program Files", "Program Files (x86)", "AppData"}

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
    except Exception:
        return []
    return roots


def iter_dev_folders(roots: List[Path]):
    for root in roots:
        for dirpath, dirnames, _ in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("$")]
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
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
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
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
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


def build_purge_categories(roots: List[Path], whitelist: Optional[List[Path]] = None,
                           cache: Optional[dict] = None,
                           workers: Optional[int] = None) -> List[Category]:
    wl = whitelist or []
    pairs = [(title, p) for title, p in iter_dev_folders(roots)
             if not (is_whitelisted(p, wl) or is_protected_path(p))]
    workers = workers or min(8, (os.cpu_count() or 4) * 2)
    sizes: Dict[Path, int] = {}
    if pairs:
        cache_lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for p, sz in pool.map(lambda pr: (pr[1], sized_dir(pr[1], cache, cache_lock)), pairs):
                sizes[p] = sz
    by_title: Dict[str, Category] = {}
    for title, p in pairs:
        cat = by_title.setdefault(title, Category(
            key=f"dev-{title}",
            title=title,
            description="project build artifacts",
            icon=DEV_CATEGORY_ICONS.get(title, "📦"),
            safe_default=True,
            scanning=False,
        ))
        cat.items.append(Item(path=p, size=sizes.get(p, 0), selected=True))
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
        for root, _, files in os.walk(d, onerror=lambda e: None):
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
                 use_cache: bool = True) -> None:
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
            for cat in build_purge_categories(self.roots or load_purge_roots() or discover_scan_roots(),
                                              self.whitelist,
                                              cache=self.cache if self.use_cache else None,
                                              workers=self.workers):
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
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory().percent
    du = psutil.disk_usage(USER.anchor or "C:\\").percent
    # lower load = higher score
    score = 100 - int((cpu * 0.3 + mem * 0.3 + du * 0.4))
    return max(0, min(100, score))


def render_status() -> Group:
    if psutil is None:
        return Group(Text("psutil not installed — `pip install psutil`", style="red"))
    cpu = psutil.cpu_percent(interval=0.1)
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
    out = Text()
    out.append("📊 Gösterge Paneli\n\n", style="bold bright_magenta")

    # ── Disk fullness ──
    if psutil is not None:
        try:
            disk = psutil.disk_usage(USER.anchor or "C:\\")
            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.1)
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
            out.append(f"({human_size(disk.free)} boş)\n", style="green")

            out.append("  RAM     ", style="grey70")
            out.append(_meter_bar(mem.percent, color=mem_color))
            out.append(f"  {mem.percent:5.1f}%", style=f"bold {mem_color}")
            out.append(f"   {human_size(mem.used)} / {human_size(mem.total)}\n", style="grey62")

            out.append("  CPU     ", style="grey70")
            out.append(_meter_bar(cpu, color="bright_blue"))
            out.append(f"  {cpu:5.1f}%\n", style="bold bright_blue")

            out.append("  Sağlık  ", style="grey70")
            out.append(_meter_bar(score, color=score_color))
            out.append(f"  {score}/100\n", style=f"bold {score_color}")
        except Exception:
            out.append("  (sistem metrikleri okunamadı)\n", style="red")
    else:
        out.append("  psutil yüklü değil — `pip install psutil`\n", style="red")

    # ── Reclaimable space from the live clean scan ──
    out.append("\n  Temizlik Analizi\n", style="bold grey85")
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
            out.append("   ⟳ taranıyor…  ", style="bright_yellow")
            out.append(f"şimdiye dek {human_size(reclaimable)} geri kazanılabilir\n", style="grey70")
        elif reclaimable > 0:
            out.append("   ♻ Geri kazanılabilir: ", style="grey70")
            out.append(f"{human_size(reclaimable)}", style="bold bright_green")
            out.append(f"  ({items} öğe)\n", style="grey62")
            out.append("   [C] Hızlı temizliği başlat\n", style="bright_cyan")
        else:
            out.append("   ✓ Temizlenecek fazla veri yok\n", style="green")
    else:
        out.append("   (analiz bekleniyor)\n", style="grey50")

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
    out.append("\n  Yapılan Temizlikler\n", style="bold grey85")
    if hist["count"] > 0:
        out.append("   Toplam boşaltılan: ", style="grey70")
        out.append(f"{human_size(hist['total_freed'])}", style="bold bright_green")
        out.append(f"   ({hist['count']} işlem)\n", style="grey62")
        if hist["last_ts"] and hist_limit > 0:
            out.append(f"   Son işlem: {hist['last_ts']}\n", style="grey62")
        for e in hist["recent"]:
            name = os.path.basename(e["path"].rstrip("\\/")) or e["path"]
            out.append(f"     {human_size(e['size']):>9}  ", style="cyan")
            out.append(f"{name[:40]}\n", style="grey70")
    else:
        out.append("   Henüz temizlik kaydı yok\n", style="grey50")

    out.append("\n  ", style="grey50")
    out.append("[C]", style="bright_cyan"); out.append(" hızlı temizlik   ", style="grey70")
    out.append("[/]", style="bright_cyan"); out.append(" komutlar   ", style="grey70")
    out.append("[S]", style="bright_cyan"); out.append(" canlı durum   ", style="grey70")
    out.append("[Q]", style="bright_cyan"); out.append(" çıkış", style="grey70")
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
    except Exception:
        return False


def _run_windows_update_reset(dry_run: bool) -> str:
    if dry_run:
        return "dry-run: would stop wuauserv, clear SoftwareDistribution, restore service state"
    was_running = _is_service_running("wuauserv")
    try:
        if was_running:
            subprocess.run(["net", "stop", "wuauserv"], capture_output=True, text=True, timeout=60, shell=False)
        subprocess.run(["cmd", "/c", "rmdir /s /q C:\\Windows\\SoftwareDistribution"], capture_output=True, text=True, timeout=120, shell=False)
        if was_running:
            subprocess.run(["net", "start", "wuauserv"], capture_output=True, text=True, timeout=60, shell=False)
        return "ok: Reset Windows Update"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return str(e)


def run_optimize(action: OptAction, dry_run: bool = False) -> str:
    if action.key == "windows-update":
        return _run_windows_update_reset(dry_run=dry_run)
    if dry_run:
        return f"dry-run: would run {action.description}"
    try:
        r = subprocess.run(action.cmd, capture_output=True, text=True, timeout=120, shell=False)
        if r.returncode == 0:
            return f"ok: {action.title}"
        return f"exit {r.returncode}: {r.stderr.strip() or r.stdout.strip()}"
    except subprocess.TimeoutExpired:
        return "timeout"
    except FileNotFoundError as e:
        return f"missing: {e}"
    except Exception as e:
        return str(e)


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
            "• Shift+K (K) ile KALICI mod açılır; başlık kırmızıya döner.",
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
            "• Shift+K toggles PERMANENT mode; header turns red.",
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
        "dashboard": "wmole · Gösterge Paneli",
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
        header.append("Sistem durumu, temizlik analizi ve geçmiş — [C] hızlı temizlik · [/] komutlar", style="grey70")
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
    except Exception:
        subprocess.Popen(["explorer", str(p)])


def confirm_delete(live: Live, scanner: Scanner, view: View, cursor: int,
                   use_trash: bool, dry_run: bool, apps: List[dict],
                   opt: List[dict]) -> str:
    # Collect selected items, partitioned into protected (blocked) and deletable.
    candidates: List[Item] = []
    if view.kind == "items" and view.category is not None:
        for it in view.category.items:
            if it.selected and not it.deleted:
                candidates.append(it)
    else:
        for cat in scanner.categories:
            for it in cat.items:
                if it.selected and not it.deleted:
                    candidates.append(it)
    protected = [it for it in candidates if is_protected_path(it.path)]
    targets   = [it for it in candidates if not is_protected_path(it.path)]
    if not targets:
        return T("nothing_selected") if not protected else T("protected_header", n=len(protected))

    total = sum(t.size for t in targets)
    if dry_run:    action_label = T("act_dry_run")
    elif use_trash: action_label = T("act_trash")
    else:           action_label = T("act_permanent")

    # Build a scrollable preview: first 10 paths, then "and N more".
    danger = (not use_trash) and (not dry_run)
    border_color = "red" if danger else ("yellow" if dry_run else "cyan")
    body_txt = Text()
    body_txt.append(T("confirm_header", n=len(targets), action=action_label, size=human_size(total)),
                    style="bold red" if danger else "bold yellow")
    body_txt.append("\n\n")
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
    except Exception:
        pass


def run_first_launch_quick_clean() -> None:
    """First-launch flow: quick-scan safe cache/temp categories, show what was
    detected, and—only after the user confirms—send it to the Recycle Bin.

    Runs once; a config flag prevents it from prompting on every start.
    """
    cfg = load_config()
    if cfg.get("quick_clean_v2_done"):
        return

    title = "wmole · İlk Açılış Temizliği"
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
            body.append("  Hızlı tarama yapılıyor…  ", style="bold white")
            body.append((scanner.status or "")[:48], style="grey62")
            partial = sum(c.total for c in scanner.categories)
            body.append(f"\n\n  Şimdiye dek bulunan: {human_size(partial)}\n", style="green")
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
            done = Text("\n  ✓ Temizlenecek fazla veri bulunamadı. Sistem temiz.\n", style="bold green")
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
        body.append("\n  Tespit edilen fazla veri: ", style="white")
        body.append(human_size(total), style="bold bright_green")
        body.append(f"   ({len(targets)} öğe)\n\n", style="grey62")
        for ttl, ct in cat_totals[:8]:
            body.append(f"   • {ttl[:28]:<28}", style="grey85")
            body.append(f"{human_size(ct):>10}\n", style="cyan")
        if len(cat_totals) > 8:
            body.append(f"   … +{len(cat_totals) - 8} kategori daha\n", style="grey50")
        body.append("\n  Seçilen öğeler Geri Dönüşüm Kutusu'na taşınacak (geri alınabilir).\n", style="grey62")
        body.append("\n  [", style="grey50"); body.append("E", style="bold bright_green")
        body.append("] Temizle     ", style="white")
        body.append("[", style="grey50"); body.append("H", style="bold red")
        body.append("] Atla", style="white")
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
            pbody.append(f"\n  Temizleniyor…  {done}/{n}\n\n", style="bold white")
            pbody.append("  [" + "█" * filled + "·" * (bar_w - filled) + "]\n", style="bright_cyan")
            pbody.append(f"\n  Boşaltıldı: {human_size(freed)}", style="green")
            live.update(Panel(pbody, title=title, border_style="cyan", padding=(1, 2)))

        res = Text()
        res.append("\n  ✓ Tamamlandı. ", style="bold green")
        res.append(human_size(freed), style="bold bright_green")
        res.append(" boşaltıldı  ", style="white")
        res.append(f"({ok} öğe", style="grey62")
        if err:
            res.append(f", {err} hata", style="red")
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
        view_stack = [View(title="Gösterge Paneli", kind="dashboard")]
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
                            view_stack = [View(title="Gösterge Paneli", kind="dashboard")]; cursor = 0
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
                    msg = run_optimize(act, dry_run=dry_run)
                elif view.kind == "uninstall":
                    app = row
                    uninst = app.get("uninstall")
                    if uninst:
                        try:
                            subprocess.Popen(uninst, shell=True)
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
        rows.append({"title": action.title, "result": run_optimize(action, dry_run=dry_run), "risk": action.risk, "admin": action.requires_admin})
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
        f'   echo [wmole] Updated to {{__version__}}. Starting...\r\n'
        f'   start "" "{current_exe}"\r\n'
        "   del \"%~f0\" & exit /b 0\r\n"
        "\r\n"
        ":fail\r\n"
        '   echo [wmole] Update failed: could not replace old exe (file locked).\r\n'
        f'   echo [wmole] New version is at: {new_exe}\r\n'
        "   timeout /t 5 >nul\r\n"
        "   del \"%~f0\" & exit /b 1\r\n"
        "endlocal\r\n"
    ).replace("{{__version__}}", __version__)
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
               quiet: bool = False) -> str:
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
            return _emit_update(steps, json_out, quiet)
        latest_tag = rel.get("tag_name", "")
        latest_ver = _parse_semver(latest_tag)
        current_ver = _parse_semver(__version__)
        steps.append({"step": "check-version", "code": 0,
                      "output": f"installed {__version__}, latest {latest_tag}"})
        if latest_ver <= current_ver:
            steps.append({"step": "decision", "code": 0,
                          "output": "already up to date"})
            return _emit_update(steps, json_out, quiet)

        if quiet:
            steps.append({"step": "decision", "code": 0,
                          "output": f"update available: {latest_tag}; restart wmole to install automatically"})
            return _emit_update(steps, json_out, quiet)

        asset = _pick_installer_asset(rel.get("assets", []))
        if not asset:
            steps.append({"step": "asset", "code": 1,
                          "output": "no installer asset found in release"})
            return _emit_update(steps, json_out, quiet)
        url = asset["browser_download_url"]
        name = Path(asset["name"]).name  # strip any path components from the API value
        steps.append({"step": "asset", "code": 0, "output": name})

        if dry_run:
            steps.append({"step": "download", "code": 0,
                          "output": f"would download {url}"})
            steps.append({"step": "install", "code": 0,
                          "output": "would run /VERYSILENT /SUPPRESSMSGBOXES /NORESTART"})
            return _emit_update(steps, json_out, quiet)

        if not yes and not json_out:
            try:
                ans = input(f"Upgrade wmole {__version__} -> {latest_tag} now? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                steps.append({"step": "decision", "code": 1, "output": "user declined"})
                return _emit_update(steps, json_out, quiet)

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
            steps.append({"step": "download", "code": 0,
                          "output": f"{dest} ({dest.stat().st_size} bytes)"})
        except Exception as exc:
            steps.append({"step": "download", "code": 1, "output": str(exc)})
            return _emit_update(steps, json_out, quiet)

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
            _emit_update(steps, json_out, quiet)
            # Exit so the installer can overwrite the running exe.
            os._exit(0)
        except Exception as exc:
            steps.append({"step": "install", "code": 1, "output": str(exc)})
            return _emit_update(steps, json_out, quiet)

    # Source checkout path (legacy)
    if path_exists(Path(".git")):
        r = subprocess.run(["git", "pull", "--ff-only"], capture_output=True, text=True, shell=False)
        steps.append({"step": "git-pull", "code": r.returncode, "output": (r.stdout or r.stderr).strip()})
    else:
        steps.append({"step": "git-pull", "code": 1, "output": "not a git repository"})
    r2 = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "rich", "send2trash", "psutil"], capture_output=True, text=True, shell=False)
    steps.append({"step": "pip-upgrade-deps", "code": r2.returncode, "output": (r2.stdout or r2.stderr).strip()[-500:]})
    return _emit_update(steps, json_out, quiet)


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


def _emit_update(steps: List[dict], json_out: bool, quiet: bool = False) -> str:
    if not quiet:
        if json_out:
            print(json.dumps({"steps": steps}, indent=2))
        else:
            for s in steps:
                print(f"{s['step']}: exit {s['code']}  {s['output']}")
    return steps[-1]["output"] if steps else ""


def cli_remove(dry_run: bool, json_out: bool) -> None:
    targets = [WMOLE_DIR]
    results = []
    for t in targets:
        err = delete_path(t, use_trash=False, dry_run=dry_run)
        results.append({"path": str(t), "result": "ok" if err is None else err})
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


def list_dev_ports(include_all: bool = False) -> List[dict]:
    """Return listening TCP/UDP sockets bound to localhost/0.0.0.0.

    include_all=True keeps every listener regardless of bind address.
    """
    if not psutil:
        return []
    rows: Dict[tuple, dict] = {}
    for kind in ("tcp", "udp"):
        try:
            conns = psutil.net_connections(kind=kind)
        except (psutil.AccessDenied, PermissionError):
            # Need admin on Windows to enumerate other users' sockets.
            try:
                conns = psutil.net_connections(kind=kind)
            except Exception:
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
                    except Exception:
                        exe = ""
                except Exception:
                    pass
            rows[key] = {
                "proto": kind.upper(),
                "ip": ip,
                "port": port,
                "pid": c.pid,
                "process": name,
                "exe": exe,
                "hint": DEV_PORT_HINTS.get(port, ""),
            }
    return sorted(rows.values(), key=lambda r: (r["port"], r["proto"]))


def kill_pid(pid: int, dry_run: bool = False, force: bool = True) -> str:
    if not pid:
        return "no pid"
    if dry_run:
        return f"would kill pid {pid}"
    if not psutil:
        return "psutil unavailable"
    try:
        proc = psutil.Process(pid)
        if force:
            proc.kill()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        return f"killed pid {pid}"
    except psutil.NoSuchProcess:
        return f"pid {pid} already gone"
    except psutil.AccessDenied:
        return f"access denied for pid {pid} (run as admin)"
    except Exception as exc:
        return f"failed pid {pid}: {exc}"


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
            msg = kill_pid(r["pid"], dry_run=dry_run)
            log_operation("port_kill", Path(f"pid:{r['pid']}:{r['proto']}:{r['port']}"),
                          0, msg)
            results.append({**r, "result": msg})
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
    print(f"{'PROTO':<5} {'PORT':>6}  {'PID':>6}  {'PROCESS':<22} {'BIND':<16} HINT")
    print("-" * 78)
    for r in rows:
        print(f"{r['proto']:<5} {r['port']:>6}  {str(r['pid'] or '-'):>6}  "
              f"{(r['process'] or '-')[:22]:<22} {r['ip']:<16} {r['hint']}")
    print()
    print("Kill: wmole ports --kill <port|port:N|pid:N|all>   (add --dry-run to preview)")


def powershell_completion_script() -> str:
    return """Register-ArgumentCompleter -CommandName wmole, py -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $modes = 'analyze','clean','purge','status','optimize','uninstall','installer','installers','update','remove','completion','ports'
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


def main_cli() -> None:
    p = argparse.ArgumentParser(prog="wmole", description="Windows port of mole")
    p.add_argument("mode", nargs="?", default="analyze",
                   choices=["analyze", "clean", "purge", "status", "optimize", "uninstall", "installer", "installers", "update", "remove", "completion", "ports"])
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
    args = p.parse_args()

    # Do not start a final background check while the user is disabling it.
    # Other commands preserve the fire-and-forget startup behavior.
    if not (args.mode == "update" and args.disable_auto):
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
    elif args.mode == "status" and args.json_out:
        if psutil:
            vm = psutil.virtual_memory(); du = psutil.disk_usage(USER.anchor or 'C:\\')
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
            print(json.dumps({
                "health": health_score(), "cpu_percent": psutil.cpu_percent(interval=0.3),
                "memory_percent": vm.percent, "memory_used": vm.used, "memory_total": vm.total,
                "disk_percent": du.percent, "disk_free": du.free, "disk_total": du.total,
                "disk_read_bytes": (dio.read_bytes if dio else 0), "disk_write_bytes": (dio.write_bytes if dio else 0),
                "net_up_bytes": net.bytes_sent, "net_down_bytes": net.bytes_recv,
                "uptime_seconds": int(time.time() - boot),
                "battery_percent": (bat.percent if bat else None), "power_plugged": (bat.power_plugged if bat else None),
                "device": {"system": sysinfo.system, "release": sysinfo.release, "node": sysinfo.node},
                "temperature_sensors": {k: [{"label": t.label, "current": t.current} for t in v[:3]] for k, v in temps.items()},
            }, indent=2))
        else:
            print("{}")
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
