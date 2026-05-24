"""
wmole — Windows port of the macOS `mole` developer-disk swiss-army knife.

Modes (toggle in the TUI with the highlighted hotkey, or pick a subcommand on
the CLI):

    analyze   disk explorer with category breakdown + select-and-delete
    purge     dev-folder bulk delete (node_modules, target, dist, …)
    clean     auto-clean *safe defaults* with a single confirmation
    status    live system dashboard (CPU/RAM/Disk/Net/Top procs/health score)
    optimize  run system maintenance actions (flush DNS, reset WU cache, …)
    uninstall list installed programs + remove with leftover sweep
    installers find old setup files in Downloads/Desktop

Safety: deletes go to the Recycle Bin by default (send2trash).
        `Shift+D` deletes permanently. `T` toggles --dry-run.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

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
COMPLETION_FILE = WMOLE_DIR / "completion.ps1"
LOG_DIR = WMOLE_DIR / "logs"
OP_LOG_FILE = LOG_DIR / "operations.log"


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
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda e: None):
            for f in files:
                try:
                    total += os.stat(os.path.join(root, f)).st_size
                    seen += 1
                except OSError:
                    pass
                if max_files is not None and seen >= max_files:
                    if on_progress:
                        on_progress(total)
                    return total
                if max_seconds is not None and time.time() - started >= max_seconds:
                    if on_progress:
                        on_progress(total)
                    return total
            if on_progress and total - last_emit > 25_000_000:
                on_progress(total)
                last_emit = total
    except Exception:
        pass
    if on_progress:
        on_progress(total)
    return total


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
    if any(raw == str(d).rstrip("\\/").lower() or str(d).rstrip("\\/").lower() in raw for d in denylist):
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


def delete_path(path: Path, use_trash: bool, dry_run: bool) -> Optional[str]:
    """Returns None on success, error string otherwise."""
    if is_protected_path(path):
        msg = "protected path"
        log_operation("delete-blocked", path, result=msg)
        return msg
    size = 0
    try:
        size = path.stat().st_size if path.is_file() else dir_size(path)
    except OSError:
        pass
    if dry_run:
        log_operation("delete-dry-run", path, size=size, result="ok")
        return None
    if use_trash and send2trash is not None:
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
        if path.is_file() or path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path, onerror=_onerr_chmod)
        log_operation("delete-permanent", path, size=size, result="ok")
        return None
    except Exception as e:
        log_operation("delete-permanent", path, size=size, result=str(e))
        return str(e)


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


def find_large_files(path: Path, minimum_size: int) -> List[dict]:
    large: List[dict] = []
    if path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size >= minimum_size:
            large.append({"name": path.name, "path": str(path), "size": size})
        return large
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            p = Path(root) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size >= minimum_size:
                large.append({"name": p.name, "path": str(p), "size": size})
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


def build_fs_category(path: Path) -> Category:
    path = path.expanduser()
    title = str(path)
    cat = Category(
        key=f"fs:{path}",
        title=title,
        description="filesystem explorer",
        icon="📁",
        safe_default=False,
        scanning=False,
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
        rows = []
        for child in children:
            if is_protected_path(child):
                continue
            rows.append(Item(path=child, size=quick_size(child), selected=False))
        rows.sort(key=lambda it: (not it.path.is_dir(), -it.size, it.path.name.lower()))
        cat.items.extend(rows)
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


def build_purge_categories(roots: List[Path], whitelist: Optional[List[Path]] = None) -> List[Category]:
    wl = whitelist or []
    by_title: Dict[str, Category] = {}
    for title, p in iter_dev_folders(roots):
        if is_whitelisted(p, wl) or is_protected_path(p):
            continue
        cat = by_title.setdefault(title, Category(
            key=f"dev-{title}",
            title=title,
            description="project build artifacts",
            icon=DEV_CATEGORY_ICONS.get(title, "📦"),
            safe_default=True,
            scanning=False,
        ))
        cat.items.append(Item(path=p, size=dir_size(p), selected=True))
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
                 profile: str = "full", roots: Optional[List[Path]] = None) -> None:
        self.categories: List[Category] = []
        self.status: str = "Initializing…"
        self.done: bool = False
        self.current_cat_key: Optional[str] = None
        self.current_item_id: Optional[int] = None
        self.whitelist = whitelist or []
        self.profile = profile
        self.roots = roots
        self._lock = threading.Lock()

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
        it.scanning = True
        self.current_item_id = id(it)
        def on_progress(n: int) -> None:
            it.partial = n
        if self.profile == "clean":
            it.size = dir_size(it.path, on_progress=on_progress, max_seconds=0.35, max_files=5000)
        else:
            it.size = dir_size(it.path, on_progress=on_progress)
        it.partial = it.size
        it.scanning = False
        self.current_item_id = None

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
            for cat in build_purge_categories(self.roots or load_purge_roots() or discover_scan_roots(), self.whitelist):
                self._add(cat)
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

        for cat in fixed_cats:
            self.status = f"Scanning {cat.title}…"
            self.current_cat_key = cat.key
            for it in cat.items:
                if it.size == 0:  # installers already have size from old_installers
                    self._size_item(it)
            cat.scanning = False
            self.current_cat_key = None

        if self.profile == "clean":
            with self._lock:
                self.categories.sort(key=lambda c: c.total, reverse=True)
            self.status = "Clean scan complete."
            self.done = True
            return

        roots = self.roots or discover_scan_roots()
        self.status = f"Searching {len(roots)} root(s) for dev folders…"
        for title, p in iter_dev_folders(roots):
            cat = dev_titles[title]
            it = Item(path=p, selected=True)
            cat.items.append(it)
            self.status = f"Sizing {p}…"
            self.current_cat_key = cat.key
            self._size_item(it)
            cat.items.sort(key=lambda i: i.size, reverse=True)
            cat.description = f"{len(cat.items)} folder(s) under your dev dirs"
            self.current_cat_key = None

        for cat in dev_titles.values():
            cat.scanning = False

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
    main = [
        "Up/Down",
        "Enter",
        "Space",
        "D",
        "Shift+D",
        "T Dry-run",
        "R Refresh",
        "O Open",
        "L Leftovers",
        "G Large",
        "V Drives",
    ]
    modes = [
        "A Analyze(FS)",
        "C Analyze(Cats)",
        "P Purge",
        "I Installers",
        "U Uninstall",
        "M Optimize",
        "S Status",
        "Esc/Q Back",
    ]
    lines = _wrap_tokens(main, max(40, width))
    lines.extend(_wrap_tokens(modes, max(40, width), prefix="Modes: "))
    return lines


@dataclass
class View:
    title: str
    kind: str   # 'cats' | 'items' | 'status' | 'optimize' | 'uninstall'
    category: Optional[Category] = None


def view_rows(view: View, scanner: Scanner) -> List[object]:
    if view.kind == "cats":
        with scanner._lock:
            return [c for c in scanner.categories if c.items or c.scanning]
    if view.kind == "items":
        return [i for i in (view.category.items if view.category else []) if not i.deleted]
    return []


def render(scanner: Scanner, view: View, cursor: int, msg: str,
           use_trash: bool, dry_run: bool, apps_cache: List[dict],
           opt_cache: List[dict]) -> Group:
    total_free = free_space_gb()
    footer_lines = build_footer_lines(console.size.width or 120)

    # Top banner — title reflects the current view
    title = {
        "cats":      "Analyze Disk",
        "items":     f"Inside · {view.category.title}" if view.category else "Analyze Disk",
        "status":    "System Status",
        "optimize":  "System Optimize",
        "uninstall": "Uninstall Programs",
    }.get(view.kind, "wmole")
    header = Text()
    header.append(title, style="bold magenta")
    header.append(f"  ({total_free:.1f} GB free)", style="grey62")
    mode_tags = []
    mode_tags.append("🗑 Trash" if use_trash else "⚠ Permanent")
    if dry_run: mode_tags.append("DRY-RUN")
    header.append("   " + "  ".join(mode_tags), style="bold yellow" if dry_run else "grey70")
    header.append("\n")
    if view.kind == "cats":
        header.append("Select cleanable categories — Space picks, D deletes selected", style="grey70")
    elif view.kind == "items":
        if view.category and view.category.key.startswith("fs:"):
            header.append("Enter open folder  ·  G large files  ·  V drives  ·  O open in Explorer  ·  Space/D delete", style="grey70")
        else:
            header.append(f"Inside  {view.category.title}  — Space picks, D deletes selected", style="grey70")
    elif view.kind == "status":
        header.append("Live system status (auto-refresh) — Q/Esc back", style="grey70")
    elif view.kind == "optimize":
        header.append("System optimize actions — Enter to run (admin may be required)", style="grey70")
    elif view.kind == "uninstall":
        header.append("Installed programs — Enter launches uninstaller, L scans leftovers", style="grey70")

    # Body
    if view.kind == "status":
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

        reserved = 9 + len(footer_lines)
        avail = max(5, (console.size.height or 30) - reserved)
        if len(rows) > avail:
            start = max(0, min(cursor - avail // 2, len(rows) - avail))
            end = start + avail
        else:
            start, end = 0, len(rows)
        rows_shown = end - start

        body = Text("\n")
        body.append((f"      ↑ {start} more above\n" if start > 0 else "\n"), style="grey42")

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
        body.append((f"      ↓ {len(rows) - end} more below\n" if end < len(rows) else "\n"), style="grey42")

    # Status / message lines (fixed 2)
    status = Text()
    line1 = ("  " + scanner.status) if (scanner.status and not scanner.done and view.kind in ("cats", "items")) else ""
    line2 = ("  " + msg) if msg else ""
    status.append((line1 or " ") + "\n", style="grey50")
    status.append((line2 or " ") + "\n", style="yellow")

    footer = Text("\n".join(footer_lines), style="bold grey70")
    return Group(header, body, status, footer)


# ---------- Actions ----------
def open_in_explorer(p: Path) -> None:
    try:
        os.startfile(str(p))  # type: ignore[attr-defined]
    except Exception:
        subprocess.Popen(["explorer", str(p)])


def confirm_delete(live: Live, scanner: Scanner, view: View, cursor: int,
                   use_trash: bool, dry_run: bool, apps: List[dict],
                   opt: List[dict]) -> str:
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
    targets = [it for it in candidates if not is_protected_path(it.path)]
    blocked = len(candidates) - len(targets)
    if not targets:
        return "Nothing selected." if not blocked else f"{blocked} protected item(s) skipped."
    total = sum(t.size for t in targets)
    verb = "DRY-RUN: would delete" if dry_run else ("Move to Recycle Bin" if use_trash else "PERMANENTLY DELETE")

    prompt = Text()
    prompt.append("  ⚠ ", style="bold red")
    prompt.append(f"{verb}  {len(targets)} item(s)", style="bold yellow")
    prompt.append(f"  totaling {human_size(total)} ?  [Y / anything = cancel]", style="grey70")
    live.update(Group(render(scanner, view, cursor, "", use_trash, dry_run, apps, opt), prompt))
    if read_key().upper() != "Y":
        return "Cancelled."

    ok = err = 0
    for it in targets:
        live.update(Group(render(scanner, view, cursor, "", use_trash, dry_run, apps, opt),
                          Text(f"  {'would delete' if dry_run else 'deleting'} {it.path} …", style="yellow")))
        e = delete_path(it.path, use_trash=use_trash, dry_run=dry_run)
        if e is None:
            if not dry_run:
                it.deleted = True
                it.selected = False
            ok += 1
        else:
            it.error = e
            err += 1
    suffix = " (dry-run, nothing removed)" if dry_run else ""
    blocked_note = f" {blocked} protected skipped." if blocked else ""
    return f"{verb}: {ok} ok, {err} errors. ~{human_size(total)}.{suffix}{blocked_note}"


# ---------- Main loop ----------
def run_tui(initial_view: str = "analyze", start_path: Optional[Path] = None) -> None:
    if os.name == "nt":
        os.system("")
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("wmole · disk cleaner")
        except Exception:
            pass

    whitelist = load_whitelist()
    cfg = load_config()
    analyze_start = start_path or Path(str(cfg.get("analyze_start_path", USER))).expanduser()
    profile = "purge" if initial_view == "purge" else ("installers" if initial_view in ("installer", "installers") else ("idle" if initial_view == "analyze" else "full"))
    scanner = Scanner(whitelist=whitelist, profile=profile)
    threading.Thread(target=scanner.run, daemon=True).start()

    apps_cache: List[dict] = []
    opt_cache: List[dict] = [a.__dict__ | {"_obj": a} for a in OPTIMIZE_ACTIONS]
    # we'll keep opt actions as the OptAction objects themselves for render:
    opt_cache = OPTIMIZE_ACTIONS  # type: ignore[assignment]

    use_trash = True
    dry_run = False
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
    else:
        view_stack = [View(title=f"Analyze · {analyze_start}", kind="items", category=build_fs_category(analyze_start))]
    cursor = 0
    msg = ""

    with Live(console=console, refresh_per_second=8, screen=True, vertical_overflow="crop") as live:
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
            live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache))

            key = None
            for _ in range(20):
                if os.name == "nt" and msvcrt.kbhit():
                    key = read_key()
                    break
                time.sleep(0.05)
                live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache))
            if key is None:
                continue
            msg = ""
            up = key.upper() if len(key) == 1 else key
            shifted = key.isalpha() and key.isupper()  # msvcrt returns upper when shift held

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
            elif up == "T":
                dry_run = not dry_run
                msg = f"Dry-run {'ON' if dry_run else 'OFF'}."
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
                        new_cat = build_fs_category(target)
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
                    live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache))
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
                large = find_large_files(base, threshold)
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
            elif up == "V" and view.kind == "items" and view.category and view.category.key.startswith("fs:"):
                drives = build_drive_picker_category()
                view_stack.append(View(title=drives.title, kind="items", category=drives))
                cursor = 0
            elif up == "SPACE" and view.kind in ("cats", "items"):
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
            elif up == "D" and view.kind in ("cats", "items"):
                permanent = shifted     # Shift+D = permanent
                msg = confirm_delete(live, scanner, view, cursor,
                                     use_trash=(not permanent) and use_trash,
                                     dry_run=dry_run, apps=apps_cache, opt=opt_cache)
            elif up == "R":
                if view.kind in ("cats", "items"):
                    if view.kind == "items" and view.category and view.category.key.startswith("fs:"):
                        base = view.category.title
                        view_stack = [View(title=f"Analyze · {base}", kind="items", category=build_fs_category(Path(base)))]
                        cursor = 0
                        msg = "Refreshed."
                    else:
                        scanner = Scanner(whitelist=whitelist, profile=profile)
                        threading.Thread(target=scanner.run, daemon=True).start()
                        view_stack = [View(title="Analyze Disk", kind="cats")]
                        cursor = 0
                        msg = "Rescanning…"
                elif view.kind == "uninstall":
                    apps_cache = list_installed_apps()
                    msg = f"Reloaded {len(apps_cache)} programs."
            # Mode switches (uppercase only — Shift required so they don't collide with selection)
            elif up == "A" and shifted:
                profile = "idle"
                scanner = Scanner(whitelist=whitelist, profile=profile)
                threading.Thread(target=scanner.run, daemon=True).start()
                view_stack = [View(title=f"Analyze · {analyze_start}", kind="items", category=build_fs_category(analyze_start))]; cursor = 0
            elif up == "P" and shifted:
                profile = "purge"
                scanner = Scanner(whitelist=whitelist, profile=profile)
                threading.Thread(target=scanner.run, daemon=True).start()
                view_stack = [View(title="Purge Artifacts", kind="cats")]; cursor = 0
            elif up == "C" and shifted:
                profile = "full"
                scanner = Scanner(whitelist=whitelist, profile=profile)
                threading.Thread(target=scanner.run, daemon=True).start()
                view_stack = [View(title="Analyze Categories", kind="cats")]; cursor = 0
            elif up == "I" and shifted:
                profile = "installers"
                scanner = Scanner(whitelist=whitelist, profile=profile)
                threading.Thread(target=scanner.run, daemon=True).start()
                view_stack = [View(title="Installers", kind="cats")]; cursor = 0
            elif up == "S" and shifted:
                view_stack = [View(title="Status", kind="status")]; cursor = 0
            elif up == "M" and shifted:
                view_stack = [View(title="Optimize", kind="optimize")]; cursor = 0
            elif up == "U" and shifted:
                if not apps_cache:
                    apps_cache = list_installed_apps()
                view_stack = [View(title="Uninstall", kind="uninstall")]; cursor = 0


# ---------- CLI ----------
def cli_clean(dry_run: bool, json_out: bool, roots: Optional[List[Path]] = None,
              whitelist: Optional[List[Path]] = None) -> None:
    """Headless: scan, pick the safe_default categories, ask once, delete."""
    wl = whitelist if whitelist is not None else load_whitelist()
    s = Scanner(whitelist=wl, profile="clean", roots=roots)
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
    return any(token in compact_name or compact_name in token for token in tokens)


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
    name = (app.get("name") or "").strip().lower()
    pub = (app.get("publisher") or "").strip().lower()
    if not name and not pub:
        return []
    targets = [t for t in {name, pub} if t]
    keys = []
    for row in list_installed_apps():
        key = str(row.get("uninstall_key") or "")
        rn = str(row.get("name") or "").lower()
        rp = str(row.get("publisher") or "").lower()
        if any(t in rn or t in rp for t in targets):
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


def cli_optimize(dry_run: bool, json_out: bool) -> None:
    rows = []
    for action in OPTIMIZE_ACTIONS:
        if action.risk == "high" and not dry_run and not json_out:
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


def cli_update(json_out: bool) -> None:
    steps = []
    if path_exists(Path(".git")):
        r = subprocess.run(["git", "pull", "--ff-only"], capture_output=True, text=True, shell=False)
        steps.append({"step": "git-pull", "code": r.returncode, "output": (r.stdout or r.stderr).strip()})
    else:
        steps.append({"step": "git-pull", "code": 1, "output": "not a git repository"})
    r2 = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "rich", "send2trash", "psutil"], capture_output=True, text=True, shell=False)
    steps.append({"step": "pip-upgrade-deps", "code": r2.returncode, "output": (r2.stdout or r2.stderr).strip()[-500:]})
    if json_out:
        print(json.dumps({"steps": steps}, indent=2))
    else:
        for s in steps:
            print(f"{s['step']}: exit {s['code']}")


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


def powershell_completion_script() -> str:
    return """Register-ArgumentCompleter -CommandName wmole, py -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $modes = 'analyze','clean','purge','status','optimize','uninstall','installer','installers','update','remove','completion'
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
                   choices=["analyze", "clean", "purge", "status", "optimize", "uninstall", "installer", "installers", "update", "remove", "completion"])
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
    args = p.parse_args()

    target_paths = [Path(t).expanduser() for t in args.targets]
    override_paths = parse_paths_arg(args.paths)
    if override_paths:
        target_paths = override_paths
    whitelist = load_whitelist(Path(args.whitelist).expanduser())

    if args.mode == "analyze" and args.json_out:
        cli_analyze(target_paths[0] if target_paths else USER, json_out=True)
    elif args.mode == "analyze" and target_paths:
        run_tui(initial_view="analyze", start_path=target_paths[0])
    elif args.mode == "clean":
        cli_clean(dry_run=args.dry_run, json_out=args.json_out, roots=target_paths or None, whitelist=whitelist)
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
        cli_optimize(dry_run=args.dry_run, json_out=args.json_out)
    elif args.mode == "uninstall" and args.json_out:
        cli_uninstall(json_out=True, query=args.query, limit=args.limit, include_leftovers=args.leftovers)
    elif args.mode == "update":
        cli_update(json_out=args.json_out)
    elif args.mode == "remove":
        cli_remove(dry_run=args.dry_run, json_out=args.json_out)
    elif args.mode == "completion":
        cli_completion(shell=args.shell, install=args.install, json_out=args.json_out)
    else:
        run_tui(initial_view=args.mode if args.mode in ("status", "optimize", "uninstall", "purge", "installer", "installers") else "analyze")


if __name__ == "__main__":
    try:
        main_cli()
    except KeyboardInterrupt:
        pass
