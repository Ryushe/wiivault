#!/usr/bin/env python3
"""
wiivault - queue Wii/GameCube ROMs from Vimm's Lair, name them with GameTDB,
and drop them into a USB Loader GX folder tree automatically.

Pipeline (per game):
    search Vimm  ->  download  ->  extract  ->  read disc ID6  ->
    look up canonical title (GameTDB)  ->  place into wbfs/ or games/

Config (remembered dirs, wit path, etc.) lives in:
    ~/.config/wiivault/config.json

Nothing here bypasses any DRM; it only fetches from the URLs you point it at,
identifies disc images, and organizes files. Only download games you own.
"""

import argparse
import base64
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

# --------------------------------------------------------------------------- #
# paths / config
# --------------------------------------------------------------------------- #

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wiivault"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "wiivault"
TITLES_CACHE = CACHE_DIR / "titles.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Vimm rotates download hosts (were downloadN.vimm.net, now dlN.vimm.net). We
# scrape the real host from each vault page's form; these are only fallbacks.
DEFAULT_DL_HOST = {
    "Wii": "dl2.vimm.net",
    "GameCube": "dl3.vimm.net",
}
FALLBACK_DL_HOST = "dl2.vimm.net"

# Nintendo disc images -> USB Loader GX (Wii wbfs / GameCube game.iso)
DISC_EXTS = {".iso", ".wbfs", ".gcm", ".rvz", ".wia", ".ciso", ".gcz", ".nkit"}
# Archives we unpack before looking inside
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2"}
# Cartridge ROMs -> emulator directory, keyed by extension -> system folder name
EMU_EXT_SYSTEM = {
    ".sfc": "SNES", ".smc": "SNES", ".swc": "SNES", ".fig": "SNES",
    ".n64": "N64", ".z64": "N64", ".v64": "N64",
    ".bin": "PS1", ".cue": "PS1", ".img": "PS1", ".pbp": "PS1", ".mdf": "PS1",
    ".nes": "NES", ".fds": "NES", ".unf": "NES", ".unif": "NES",
    ".gb": "GB", ".gbc": "GBC", ".gba": "GBA",
    ".md": "Genesis", ".gen": "Genesis", ".smd": "Genesis", ".sg": "Genesis",
    ".sms": "MasterSystem", ".gg": "GameGear",
    ".pce": "PCEngine", ".a26": "Atari2600", ".lnx": "Lynx",
    ".ws": "WonderSwan", ".wsc": "WonderSwan", ".ngp": "NeoGeoPocket",
    ".col": "ColecoVision", ".vb": "VirtualBoy",
}
# Each Wii emulator reads ROMs from its OWN folder at the drive root, not a
# generic roms/<System>/. These are the defaults for the emulators in use;
# override per-system in config["emu_paths"]. Unlisted systems fall back to
# roms/<System>.
EMU_PATHS = {
    "SNES": "snes9xgx/roms",     # Snes9x GX  (.smc .sfc .swc .fig)
    "N64":  "wii64/roms",        # Wii64 / Not64  (.z64 .n64 .v64)
    "PS1":  "wiisx/isos",        # WiiSX  (.bin/.cue .img)
}
ROM_ALL = DISC_EXTS | set(EMU_EXT_SYSTEM)

# FAT32 cannot hold a single file larger than this (4 GiB - 1 byte)
FAT32_MAX = 4 * 1024**3 - 1

DEFAULTS = {
    "wii_dir": "/mnt/h",                       # root that contains  wbfs/   (H:\ on Windows)
    "gc_dir": "/mnt/h",                        # root that contains  games/
    # USB Loader GX's own default image tree, so no custom paths are needed on
    # the Wii. 3D covers live in the ROOT of images/, the rest in subfolders.
    "covers_dir": "/mnt/h/apps/usbloader_gx/images",
    "emu_dir": "/mnt/h",                       # drive root; retro goes to <emu_dir>/<emulator path>/
    "emu_paths": {},                           # per-system override of EMU_PATHS
    "download_dir": str(CACHE_DIR / "downloads"),
    "region": "US",                            # preferred region when a name matches many
    "lang": "EN",
    "wit": "wit",                              # Wiimms ISO Tools (iso<->wbfs, split)
    "nkit": "nkit",                            # Nanook/NKit (.NET 6) to restore .nkit.iso
    "convert_to_wbfs": True,                   # iso -> wbfs for Wii when wit present
    "download_covers": True,                   # box + disc art fetched on every install
    # FAT32's max file is 4 GiB - 1 byte, so "4G" (exactly 4 GiB) is one byte
    # too large. 4095M stays under the limit while wasting almost nothing.
    "split_size": "4095M",
}

# GameTDB art server. GameCube art lives under the "wii" path too (verified).
ART_BASE = "https://art.gametdb.com/wii"
# GameTDB cover type -> subfolder under covers_dir (files named <ID6>.png).
# These match USB Loader GX's default image tree: 3D covers sit in the root of
# images/, with 2D/, full/ and disc/ beside them.
COVER_TYPES = {
    "cover3D":   "",       # angled box render -> images/<ID6>.png  (the default)
    "cover":     "2D",     # flat front only   -> images/2D/
    "coverfull": "full",   # full flat box     -> images/full/
    "disc":      "disc",   # disc label        -> images/disc/
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            warn(f"could not read config ({e}); using defaults")
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def apply_out(cfg, out):
    """Per-run override of the output root: <out>/wbfs, /games, /covers, /roms.
       Leaves the saved config untouched."""
    if out:
        out = str(Path(out).expanduser())
        cfg = dict(cfg)
        cfg["wii_dir"] = cfg["gc_dir"] = out
        cfg["covers_dir"] = str(Path(out) / "covers")
        cfg["emu_dir"] = out
    return cfg


# --------------------------------------------------------------------------- #
# small ui helpers
# --------------------------------------------------------------------------- #

def info(msg):  print(f"  {msg}")
def step(msg):  print(f"\033[1m==>\033[0m {msg}")
def ok(msg):    print(f"\033[32m ok\033[0m {msg}")
def warn(msg):  print(f"\033[33m  !\033[0m {msg}", file=sys.stderr)
def die(msg):   print(f"\033[31merr\033[0m {msg}", file=sys.stderr); sys.exit(1)


# In parallel mode two threads print at once; suppress the noisy sub-outputs
# (download progress bar, wit's chatter) so only single-line status remains.
QUIET = False


def free_gib(cfg):
    """Free space (GiB) on the output drive; inf if it can't be read."""
    try:
        return shutil.disk_usage(cfg["wii_dir"]).free / 1024**3
    except OSError:
        return float("inf")


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

# One cookie jar for the whole run: Vimm sets a session on the vault page that
# the download request is expected to carry.
_COOKIES = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIES))


def http_get(url, referer=None, binary=False):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with _OPENER.open(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", "replace")


def normalize(s):
    """Lowercase, strip punctuation/region noise for fuzzy comparison."""
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # (Demo) [!] etc.
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def ratio(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


# --------------------------------------------------------------------------- #
# GameTDB title database
# --------------------------------------------------------------------------- #

def ensure_titles(cfg, force=False):
    """Download + cache GameTDB titles.txt. Returns {ID6: title}."""
    if force or not TITLES_CACHE.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        url = f"https://www.gametdb.com/titles.txt?LANG={cfg['lang']}"
        step(f"fetching GameTDB title database ({cfg['lang']})")
        TITLES_CACHE.write_bytes(http_get(url, binary=True))
    titles = {}
    for line in TITLES_CACHE.read_text("utf-8", "replace").splitlines():
        if " = " in line:
            gid, name = line.split(" = ", 1)
            titles[gid.strip()] = name.strip()
    return titles


def title_for(id6, titles):
    if id6 and id6 in titles:
        return titles[id6]
    # ID6 may be a region-swap of a known title (last-but-one char is region)
    if id6:
        for gid, name in titles.items():
            if gid[:3] == id6[:3] and gid[4:] == id6[4:]:
                return name
    return None


# --------------------------------------------------------------------------- #
# cover / disc art  (GameTDB), WBM-style
# --------------------------------------------------------------------------- #

def region_candidates(id6):
    """Language folders to try for GameTDB art, based on the ID6 region char."""
    r = id6[3] if len(id6) >= 4 else "E"
    table = {
        "E": ["US"], "N": ["US"],                         # Americas
        "J": ["JA", "US"],                                # Japan
        "K": ["KO", "US"],                                # Korea
        "W": ["ZHTW", "US"],                              # Taiwan
        "D": ["DE", "EN", "US"], "F": ["FR", "EN", "US"], # PAL locales
        "S": ["ES", "EN", "US"], "I": ["IT", "EN", "US"],
        "H": ["NL", "EN", "US"], "R": ["RU", "EN", "US"],
    }
    return table.get(r, ["EN", "US"])                     # P/X/Y/Z/U and unknowns


def _get_png(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError:
        return None


def download_covers(id6, cfg, save_as=None):
    """Fetch 2D/3D/Full/Disc art for one game into the USB Loader GX cover tree.
       `save_as` files the art under a different id than it was fetched with —
       used for modded builds that borrow the base game's artwork."""
    base = Path(cfg["covers_dir"])
    name = save_as or id6
    regions = region_candidates(id6)
    got = []
    for art_type, sub in COVER_TYPES.items():
        dest = (base / sub / f"{name}.png") if sub else (base / f"{name}.png")
        label = sub or "3D"   # 3D covers sit in the root, so `sub` is "" — name it
        if dest.exists():
            got.append(label)
            continue
        for region in regions:
            data = _get_png(f"{ART_BASE}/{art_type}/{region}/{id6}.png")
            if data and data[:4] == b"\x89PNG":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                got.append(label)
                break
    if got:
        ok(f"covers [{name}]{f' (art from {id6})' if save_as and save_as != id6 else ''}"
           f": {', '.join(got)}")
    else:
        warn(f"no GameTDB art found for {id6}")


# --------------------------------------------------------------------------- #
# NKit restore  (.nkit.iso -> full .iso)
# --------------------------------------------------------------------------- #

def is_nkit(path):
    p = Path(path)
    if ".nkit." in p.name.lower():
        return True
    try:
        with open(p, "rb") as f:
            return b"NKIT" in f.read(0x8000)
    except OSError:
        return False


def denkit(src, cfg):
    """Restore an NKit image to a full .iso. Returns the new path, or None."""
    src = Path(src)
    if not shutil.which(cfg["nkit"]):
        warn(f"'{cfg['nkit']}' not found — cannot restore NKit image "
             f"{src.name}. Install Nanook/NKit (.NET 6) or set: "
             f"config --nkit <path>. Leaving the file as-is.")
        return None
    out = Path(cfg["download_dir"]) / (re.sub(r"\.nkit$", "", src.stem) + ".iso")
    out.parent.mkdir(parents=True, exist_ok=True)
    step(f"NKit restore -> ISO: {src.name}")
    try:
        subprocess.run([cfg["nkit"], "-task", "convert", str(src), str(out)],
                       check=True)
    except (subprocess.SubprocessError, OSError) as e:
        warn(f"NKit conversion failed ({e}); try '-task recover' manually.")
        return None
    return out if out.exists() and out.stat().st_size > 1024 else None


# --------------------------------------------------------------------------- #
# disc identification  (read ID6 + system straight from the image header)
# --------------------------------------------------------------------------- #

WII_MAGIC = b"\x5d\x1c\x9e\xa3"   # at disc offset 0x18
GC_MAGIC = b"\xc2\x33\x9f\x3d"    # at disc offset 0x1c


def read_disc_info(path, cfg):
    """Return (id6, kind) where kind in {'wii','gc','unknown'}.
       The raw disc header sits at a different offset per container:
         .iso/.gcm  -> 0            .wbfs -> 1 << hdr[8] (usually 0x200)
         .ciso      -> 0x8000       (CISO header + block map, then block 0)"""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if ext == ".wbfs" or magic == b"WBFS":
                f.seek(0)
                head = f.read(12)
                base = (1 << head[8]) if head[:4] == b"WBFS" else 0x200
            elif magic == b"CISO":
                base = 0x8000          # first data block = start of the disc
            else:
                base = 0
            f.seek(base)
            disc = f.read(0x20)
    except OSError:
        return None, "unknown"

    id6 = None
    kind = "unknown"
    if len(disc) >= 0x20:
        cand = disc[:6].decode("ascii", "ignore")
        if re.fullmatch(r"[A-Z0-9]{6}", cand):
            id6 = cand
        if disc[0x18:0x1c] == WII_MAGIC:
            kind = "wii"
        elif disc[0x1c:0x20] == GC_MAGIC:
            kind = "gc"

    # containers we can't read raw (rvz/wia/ciso/gcz): ask wit if available
    if id6 is None and shutil.which(cfg["wit"]):
        try:
            out = subprocess.run([cfg["wit"], "ID6", str(path)],
                                 capture_output=True, text=True, timeout=120)
            m = re.search(r"[A-Z0-9]{6}", out.stdout)
            if m:
                id6 = m.group(0)
        except (subprocess.SubprocessError, OSError):
            pass

    if kind == "unknown" and ext == ".wbfs":
        kind = "wii"
    return id6, kind


# --------------------------------------------------------------------------- #
# Vimm search + download
# --------------------------------------------------------------------------- #

def clean_query(s):
    """Strip special characters that trip up Vimm's search; keep words + spaces.
       So "Zelda: The Wind Waker!" -> "Zelda The Wind Waker"."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def vimm_search(query, system):
    url = (f"https://vimm.net/vault/?p=list&system={urllib.parse.quote(system)}"
           f"&q={urllib.parse.quote(clean_query(query))}")
    html = http_get(url, referer="https://vimm.net/")
    results = []
    seen = set()
    # real rows look like:  <a href= "/vault/63650" onmouseover="...">Mario Kart Wii</a>
    # note the space after href= , and empty decoy anchors (/vault/999999) we skip.
    for m in re.finditer(r'href=\s*"/vault/(\d+)"[^>]*>\s*([^<]+?)\s*</a>', html):
        vid, title = m.group(1), m.group(2).strip()
        if vid in seen or not title:
            continue
        seen.add(vid)
        # best-effort region: /images/flags/<region>.png sits later in the row
        region = ""
        tail = html[m.end():m.end() + 500]
        flags = re.findall(r'/images/flags/([A-Za-z]+)\.', tail[:tail.find("/vault/") if "/vault/" in tail else len(tail)])
        if flags:
            region = ",".join(dict.fromkeys(flags))
        results.append({"vault": vid, "title": title, "region": region})
    return results


def find_media(vault_id, system):
    """Return (discs, download_url) for a vault page.

       Every vault page embeds a `media` JSON array with one entry per disc:
       {"ID": <mediaId>, "GoodTitle": <base64 filename>, "SortOrder": n, ...}
       Multi-disc games (Resident Evil 4) list each disc separately, and bonus
       discs may carry a different game code entirely — we just download them
       all and let the ID6 read off each image decide where it lands.

       Vimm rotates download hosts (was downloadN, now dlN) so the host is
       scraped from the form's own action rather than assumed."""
    page = http_get(f"https://vimm.net/vault/{vault_id}",
                    referer="https://vimm.net/")

    # the download form is the one whose action points at a vimm download host
    form_media = None
    download_url = None
    for m in re.finditer(r'<form\b[^>]*action="([^"]*)"[^>]*>(.*?)</form>', page, re.S):
        action, body = m.group(1), m.group(2)
        if "mediaId" not in body:
            continue
        mid = re.search(r'name="mediaId"\s+value="(\d+)"', body)
        if mid:
            form_media = mid.group(1)
        if action.startswith("//"):
            action = "https:" + action
        elif action.startswith("/") or not action:
            action = None                          # not an off-site download url
        download_url = action
        break

    discs = []
    m = re.search(r'(?:var|const|let)\s+media\s*=\s*(\[.*?\]);', page, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            data = []
        for i, e in enumerate(data, 1):
            if e.get("ID") is None:
                continue
            try:
                title = base64.b64decode(e.get("GoodTitle", "")).decode("utf-8", "replace")
            except Exception:
                title = ""
            discs.append({"id": str(e["ID"]), "title": title,
                          "sort": int(e.get("SortOrder") or i),
                          "size": e.get("ZippedText") or ""})
    if not discs:                                  # single disc / no media array
        discs = [{"id": form_media or str(vault_id), "title": "", "sort": 1, "size": ""}]
    discs.sort(key=lambda d: d["sort"])
    if not download_url:
        download_url = f"https://{DEFAULT_DL_HOST.get(system, FALLBACK_DL_HOST)}/"
    return discs, download_url


def vimm_download(vault_id, system, cfg, alt="0", host_override=None,
                  media_id=None, url=None):
    """Vimm's download is a GET of  https://<dlhost>/?mediaId=<id>  carrying the
       session cookie from the vault page. (The on-page form says POST, but its
       submitDL() handler turns it into this GET.) Returns (path, system) or
       (None, None) on failure so a queue can keep going / stop cleanly."""
    if media_id is None or url is None:
        discs, url = find_media(vault_id, system)
        media_id = discs[0]["id"]
    detected = system
    if host_override:
        url = f"https://{host_override}/"
    dl_dir = Path(cfg["download_dir"])
    dl_dir.mkdir(parents=True, exist_ok=True)

    url = f"{url.rstrip('/')}/?mediaId={urllib.parse.quote(str(media_id))}"
    if str(alt) not in ("0", "", "None"):
        url += f"&alt={urllib.parse.quote(str(alt))}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://vimm.net/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    step(f"downloading vault {vault_id} (mediaId={media_id}) from {url}")
    try:
        resp = _OPENER.open(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            warn("Vimm rate limit (HTTP 429). They allow only a limited number "
                 "of downloads in a window — wait a few minutes and re-run; "
                 "already-installed games are skipped.")
        elif e.code == 400:
            warn("Vimm returned 400. The download flow likely changed again "
                 "(check the vault page's form/submitDL), or the session cookie "
                 "was rejected.")
        else:
            warn(f"Vimm returned HTTP {e.code} for vault {vault_id}.")
        return None, None
    except urllib.error.URLError as e:
        warn(f"could not reach Vimm ({e.reason}) for vault {vault_id}.")
        return None, None

    cd = resp.headers.get("Content-Disposition", "")
    fn = re.search(r'filename="?([^"]+)"?', cd)
    filename = fn.group(1) if fn else f"vault_{vault_id}.zip"
    filename = re.sub(r"[^\w.\- ]", "_", filename)
    dest = dl_dir / filename

    total = int(resp.headers.get("Content-Length", 0))
    # already fully downloaded? don't pull it again (re-runs stay cheap, and it
    # keeps us away from Vimm's rate limit)
    if total and dest.exists() and dest.stat().st_size == total:
        resp.close()
        ok(f"already downloaded, reusing {dest.name}")
        return dest, detected
    done = 0
    with open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and not QUIET:
                pct = done * 100 // total
                print(f"\r  {done >> 20} / {total >> 20} MiB ({pct}%)", end="")
    if not QUIET:
        print()
    if dest.stat().st_size < 1024:
        warn(f"download is suspiciously small ({dest.stat().st_size} bytes); "
             f"vimm may have served an error page.")
    ok(f"saved {dest}")
    return dest, detected


# --------------------------------------------------------------------------- #
# extraction + placement
# --------------------------------------------------------------------------- #

def unpack(path, cfg):
    """Return the list of ROM files reachable from `path`:
       - a bare ROM file        -> [it]
       - an archive (zip/7z/..) -> extract to a temp dir, return ROMs inside
       - anything else          -> []"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in ROM_ALL or is_nkit(path):
        return [path]
    if ext not in ARCHIVE_EXTS:
        return []

    out = Path(cfg["download_dir"]) / "extract" / path.stem
    out.mkdir(parents=True, exist_ok=True)
    step(f"extracting {path.name}")
    try:
        if ext == ".zip":
            with zipfile.ZipFile(path) as z:
                z.extractall(out)
        elif shutil.which("7z"):
            subprocess.run(["7z", "x", "-y", f"-o{out}", str(path)],
                           check=True, stdout=subprocess.DEVNULL)
        elif ext == ".rar" and shutil.which("unrar"):
            subprocess.run(["unrar", "x", "-y", str(path), str(out) + "/"],
                           check=True, stdout=subprocess.DEVNULL)
        else:
            warn(f"can't extract {ext} (install 7z); skipping {path.name}")
            return []
    except (zipfile.BadZipFile, subprocess.SubprocessError, OSError) as e:
        warn(f"extract failed for {path.name}: {e}")
        return []

    roms = [p for p in out.rglob("*")
            if p.is_file() and (p.suffix.lower() in ROM_ALL or is_nkit(p))]
    if not roms:
        warn(f"no ROMs found inside {path.name}")
    return roms


def cleanup_download(archive, cfg):
    """After a game is safely on the drive, remove its cached archive and the
       uncompressed extraction so each game lives in exactly one place. Only
       touches files under our own download_dir — never the user's own files
       passed to `import`. Kept on failure so a re-run can resume."""
    dl = Path(cfg["download_dir"]).resolve()
    archive = Path(archive)
    extract_dir = dl / "extract" / archive.stem
    for p in (extract_dir, archive):
        try:
            if dl not in p.resolve().parents and p.resolve() != dl:
                continue                                # safety: stay inside cache
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError as e:
            warn(f"couldn't clean up {p.name}: {e}")


def gather(paths, dirs, cfg):
    """Flatten files, archives, and folders into a list of ROM file paths."""
    roms = []
    for t in list(paths) + list(dirs):
        p = Path(t).expanduser()
        if not p.exists():
            warn(f"not found: {p}")
            continue
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and (f.suffix.lower() in ROM_ALL
                                    or f.suffix.lower() in ARCHIVE_EXTS):
                    roms += unpack(f, cfg)
        else:
            roms += unpack(p, cfg)
    return roms


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*]', "", name).rstrip(". ")
    return name or "Unknown"


def wit_copy(cfg, src, dest, wbfs, patch_id=None, patch_name=None):
    """Convert with Wiimms ISO Tools. Returns True on success.
       Only WBFS gets split: FAT32 needs <4GB parts for Wii, but GameCube must
       stay a single game.iso for Nintendont. Note wit wants '4G', not '4gb' —
       an invalid size is a syntax error (exit 108), not a conversion failure.

       patch_id/patch_name rewrite the ID and title *inside* the disc image
       (--modify all covers disc header, boot, ticket and tmd). This is what
       makes a modded game a distinct entry with its own saves, rather than
       colliding with the original."""
    fmt = "--wbfs" if wbfs else "--iso"
    cmd = [cfg["wit"], "COPY", str(src), str(dest), fmt, "--overwrite"]
    if wbfs:
        cmd += ["--split", "--split-size", str(cfg["split_size"])]
    else:
        # GameCube can't be split (Nintendont needs one game.iso), so an image
        # whose declared geometry is Wii-sized would blow past FAT32's 4 GiB
        # file cap even when the real content is small. --trunc writes the
        # actual disc size instead of the padded one.
        cmd += ["--trunc"]
    if patch_id or patch_name:
        cmd += ["--modify", "all"]
        if patch_id:
            cmd += ["--id", patch_id]
        if patch_name:
            cmd += ["--name", patch_name[:63]]     # disc title field is limited
    step(f"wit {'->wbfs' if wbfs else '->iso'}: {dest.name}"
         + (f"  (patching id={patch_id})" if patch_id else ""))
    try:
        subprocess.run(cmd, check=True,
                       **({"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                          if QUIET else {}))
        return True
    except subprocess.CalledProcessError as e:
        warn(f"wit failed (exit {e.returncode}) on {Path(src).name}. "
             f"{'Check --split-size (wit wants e.g. 4G). ' if e.returncode == 108 else ''}"
             f"Leaving this game uninstalled.")
        return False
    except OSError as e:
        warn(f"could not run '{cfg['wit']}': {e}")
        return False


def dest_for(id6, kind, title, cfg, disc_no=1):
    """Canonical output path for a disc — the single source of truth shared by
       install() and the 'already installed?' check, so they can't drift.
       Returns (root, folder, dest, target)."""
    if kind == "gc":
        root = Path(cfg["gc_dir"]) / "games"
        folder = root / f"{safe_name(title)} [{id6}]"
        # Nintendont: disc 1 is game.iso, extra discs sit beside it as discN.iso
        dest = folder / ("game.iso" if disc_no <= 1 else f"disc{disc_no}.iso")
        target = "iso"          # GameCube MUST be plain ISO
    else:                       # wii (default)
        root = Path(cfg["wii_dir"]) / "wbfs"
        folder = root / f"{safe_name(title)} [{id6}]"
        dest = folder / (f"{id6}.wbfs" if disc_no <= 1 else f"{id6}_disc{disc_no}.wbfs")
        target = "wbfs"
    return root, folder, dest, target


def is_installed(id6, kind, cfg, disc_no=1, min_bytes=1 << 20):
    """Authoritative 'is this disc already on the drive?' check. The filesystem
       is the source of truth (never a cache): if the user deleted a game by
       hand it must re-install. Matches by the stable [ID6] folder tag, not the
       human title, and globs so a renamed folder still counts."""
    root = (Path(cfg["gc_dir"]) / "games") if kind == "gc" else (Path(cfg["wii_dir"]) / "wbfs")
    if not root.is_dir():
        return None
    if kind == "gc":
        fname = "game.iso" if disc_no <= 1 else f"disc{disc_no}.iso"
    else:
        fname = f"{id6}.wbfs" if disc_no <= 1 else f"{id6}_disc{disc_no}.wbfs"
    tag = f"[{id6}]"                                        # NB: not a glob — the
    for folder in root.iterdir():                          # brackets would be a
        if not folder.is_dir() or not folder.name.endswith(tag):  # char class
            continue
        f = folder / fname
        if f.exists() and f.stat().st_size >= min_bytes:
            return f
        # wit writes split .wbfs as ID6.wbfs (+ .wbf1); the base part suffices
    return None


# --- library ledger: vault_id -> id6 cache (accelerator only, never truth) ---

def _ledger_path(cfg):
    """Prefer a per-drive ledger that travels with the disk; fall back to the
       config dir if the out root isn't writable."""
    root = Path(cfg["wii_dir"])
    try:
        d = root / ".wiivault"
        d.mkdir(parents=True, exist_ok=True)
        if os.access(d, os.W_OK):
            return d / "library.json"
    except OSError:
        pass
    return CONFIG_DIR / "library.json"


def load_ledger(cfg):
    p = _ledger_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    return {}


def ledger_add_disc(cfg, vault_id, system, title, id6, kind, disc_no, n_discs=1):
    """Append one installed disc to a vault's ledger record. A vault can hold
       several discs with DIFFERENT ids (RE4: G4BE08 discs 1-2 + a D4BE08 preview
       disc), so the entry stores a list — one id6 per vault would be overwritten
       by a bonus disc and mis-report completeness. `n_discs` is the vault's total
       so a partial install (e.g. `--disc 1`) isn't mistaken for the whole set."""
    if not vault_id:
        return
    led = load_ledger(cfg)
    entry = led.get(str(vault_id))
    if not isinstance(entry, dict) or "discs" not in entry:   # new / legacy
        entry = {"system": system, "title": title, "discs": []}
    rec = {"id6": id6, "kind": kind, "disc_no": disc_no}
    if rec not in entry["discs"]:
        entry["discs"].append(rec)
    entry["title"] = title or entry.get("title", "")
    entry["n_discs"] = max(n_discs, entry.get("n_discs", 0), len(entry["discs"]))
    entry["installed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    led[str(vault_id)] = entry
    try:
        _ledger_path(cfg).write_text(json.dumps(led, indent=2))
    except OSError as e:
        warn(f"couldn't write library ledger: {e}")


def ledger_complete(cfg, vault_id):
    """Return the vault's entry only if we recorded ALL its discs AND every one
       is still on the drive — else None. Guards both a partially-installed
       multi-disc game and a deliberate single-disc (`--disc N`) subset."""
    entry = load_ledger(cfg).get(str(vault_id))
    if not isinstance(entry, dict) or not entry.get("discs"):
        return None
    if len(entry["discs"]) < entry.get("n_discs", len(entry["discs"])):
        return None                                    # not all discs recorded
    for d in entry["discs"]:
        if not is_installed(d["id6"], d["kind"], cfg, d.get("disc_no", 1)):
            return None
    return entry


# --- cross-process claims: stop two runs transferring the same game at once ---
# Files live in the LOCAL config dir (reliable O_EXCL) even when the drive is a
# 9p/drvfs mount. A claim records pid+time so a crashed run's claim is reclaimed.
CLAIMS_DIR = CONFIG_DIR / "claims"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                    # exists, owned by someone else
    except OSError:
        return False


def claim(key, stale_after=6 * 3600):
    """Reserve `key` for this process. Returns True if we now own it, False if
       another LIVE, non-stale process already holds it. Atomic via O_EXCL."""
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    path = CLAIMS_DIR / (re.sub(r"[^A-Za-z0-9._-]", "_", str(key)) + ".claim")
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time()}).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                info = json.loads(path.read_text())
            except (ValueError, OSError):
                info = {}
            if info.get("pid") == os.getpid():
                return True
            live = info.get("pid") and _pid_alive(info["pid"])
            fresh = (time.time() - info.get("at", 0)) < stale_after
            if live and fresh:
                return False                           # someone else is on it
            try:
                path.unlink()                          # stale -> steal and retry
            except OSError:
                return False
    return False


def release(key):
    try:
        (CLAIMS_DIR / (re.sub(r"[^A-Za-z0-9._-]", "_", str(key)) + ".claim")).unlink()
    except OSError:
        pass


def _drive_tag(cfg):
    return re.sub(r"[^A-Za-z0-9]", "_", str(Path(cfg["wii_dir"]).resolve()))


def install(src, id6, kind, cfg, dry_run=False, covers=None, disc_no=1,
            custom_id=None, custom_title=None, cover_id=None, force=False):
    """Place a disc image into the USB Loader GX tree in the format that boots:
       Wii -> wbfs/Title [ID6]/ID6.wbfs   GameCube -> games/Title [ID6]/game.iso
       Restores NKit images to ISO first, then fetches cover/disc art.
       Skips the copy if the disc is already installed (unless force)."""
    titles = install._titles
    have_wit = bool(shutil.which(cfg["wit"]))
    if covers is None:
        covers = cfg.get("download_covers", True)

    # A custom/modded build: the given ID replaces the disc's own, and is what
    # the folder, filename and (patched) disc header all use.
    real_id = id6
    if custom_id:
        id6 = custom_id
    title = custom_title or title_for(id6, titles) or Path(src).stem
    art_id = cover_id or id6

    # dedup: the effective (possibly custom) id6 is what identifies the game
    existing = is_installed(id6, kind, cfg, disc_no)
    if existing and not force:
        verb = "would skip (already installed)" if dry_run else "already installed, skipping"
        ok(f"{verb} {title} [{id6}]" + (f" disc {disc_no}" if disc_no > 1 else ""))
        if covers and not dry_run:                 # still backfill missing art
            download_covers(art_id, cfg, save_as=id6)
        return existing

    _, folder, dest, target = dest_for(id6, kind, title, cfg, disc_no)
    if disc_no > 2 and kind == "gc":
        warn(f"disc {disc_no}: Nintendont officially supports game.iso + disc2.iso "
             f"only; writing {dest.name} — you may need to swap it in manually.")

    print()
    step(f"{title}  [{id6}]  ({'GameCube' if kind == 'gc' else 'Wii'})  -> .{target}")
    if custom_id:
        info(f"custom id: disc header {real_id or '?'} -> {id6}"
             + ("" if have_wit else "   (NOT patched: wit missing)"))
    info(f"-> {dest}")
    if dry_run:
        if covers:
            info(f"covers <- GameTDB {art_id}  ->  {cfg['covers_dir']}/…/{id6}.png")
        return dest

    # claim this game so a concurrent run can't transfer/write it at the same
    # time (split .wbfs parts written twice would corrupt), then re-check under
    # the claim in case the other run just finished it.
    claim_key = f"{_drive_tag(cfg)}:{id6}:{disc_no}"
    if not claim(claim_key):
        warn(f"another wiivault run is transferring {title} [{id6}]"
             + (f" disc {disc_no}" if disc_no > 1 else "") + " — skipping (concurrency)")
        return None
    try:
        existing = is_installed(id6, kind, cfg, disc_no)
        if existing and not force:
            ok(f"already installed (by a concurrent run), skipping {title} [{id6}]")
            if covers:
                download_covers(art_id, cfg, save_as=id6)
            return existing

        src = Path(src)
        tmp_iso = None
        if is_nkit(src):
            restored = denkit(src, cfg)
            if restored is None:
                return None                    # can't make it bootable; leave it
            src, tmp_iso = restored, restored

        folder.mkdir(parents=True, exist_ok=True)
        src_ext = src.suffix.lower()
        big = src.stat().st_size > 4 * 1024**3

        # a custom id must be written into the disc itself, or the loader still
        # reads the original and treats it as the same game (shared saves)
        patch_id = custom_id if (custom_id and custom_id != real_id) else None
        patch_name = custom_title if custom_id else None
        if patch_id and not have_wit:
            warn(f"'{cfg['wit']}' not found — cannot patch the disc id to {id6}. "
                 f"The folder will say {id6} but the loader will still read "
                 f"{real_id or 'the original id'}.")

        if target == "wbfs":
            needs_wit = src_ext != ".wbfs" or big or patch_id   # convert/split/patch
            if needs_wit and have_wit:
                if not wit_copy(cfg, src, dest, wbfs=True,
                                patch_id=patch_id, patch_name=patch_name):
                    return None
            elif needs_wit:
                warn(f"'{cfg['wit']}' not found — can't {'split ' if big else ''}"
                     f"make wbfs. Copying raw {src_ext}; convert with wit/WBM later.")
                shutil.copyfile(src, folder / f"{id6}{src_ext}")
            else:
                step(f"copying {dest.name}"); shutil.copyfile(src, dest)
        else:  # GameCube -> game.iso
            needs_wit = src_ext not in (".iso", ".gcm") or patch_id
            if needs_wit and have_wit:
                if not wit_copy(cfg, src, dest, wbfs=False,
                                patch_id=patch_id, patch_name=patch_name):
                    return None
            elif needs_wit:
                warn(f"'{cfg['wit']}' not found — can't convert {src_ext} to iso. "
                     f"Copying raw; convert it to game.iso later.")
                shutil.copyfile(src, folder / f"game{src_ext}")
            else:
                step(f"copying {dest.name}"); shutil.copyfile(src, dest)

        # FAT32 caps a single file at 4 GiB. Wii images get split, but GameCube
        # must stay one game.iso — flag an oversized one rather than let it fail
        # mid-write with a misleading "no space left on device".
        if dest.exists() and target == "iso" and dest.stat().st_size > FAT32_MAX:
            warn(f"{dest.name} is {dest.stat().st_size / 1024**3:.2f} GiB, over "
                 f"FAT32's 4 GiB per-file limit. It will not work on a FAT32 drive; "
                 f"reformat to exFAT or keep this title as .ciso (Nintendont reads "
                 f"CISO natively).")

        ok(f"installed {dest}")
        if tmp_iso and tmp_iso.exists():
            tmp_iso.unlink()                            # drop the restored ISO
        if covers:
            # art comes from GameTDB under art_id (a mod can borrow the base
            # game's art) but is filed under the id the loader actually reads
            download_covers(art_id, cfg, save_as=id6)
        return dest
    finally:
        release(claim_key)


def emu_rel(system, cfg):
    """Relative folder a retro ROM goes in, for the configured emulator."""
    paths = {**EMU_PATHS, **cfg.get("emu_paths", {})}
    return paths.get(system, f"roms/{system}")


def install_emu(src, system, cfg, dry_run=False, force=False):
    """Copy a retro ROM into its emulator's folder at the drive root, e.g.
       SNES -> <drive>/snes9xgx/roms/. Filename kept (retro loaders match by
       name/DAT). Skips if already there (same size) unless force."""
    src = Path(src)
    dest = Path(cfg["emu_dir"]) / emu_rel(system, cfg) / src.name
    print()
    step(f"{src.name}  [{system}]  -> {emu_rel(system, cfg)}/")
    info(f"-> {dest}")
    if dry_run:
        return dest
    if dest.exists() and dest.stat().st_size == src.stat().st_size and not force:
        ok(f"already installed, skipping {src.name} [{system}]")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    ok(f"installed {dest}")
    return dest


def covers_flag(args):
    """Tri-state: True (--covers), False (--no-covers), None (use config)."""
    if getattr(args, "covers", False):
        return True
    if getattr(args, "no_covers", False):
        return False
    return None


def derive_mod_id(base_id6, suffix="99"):
    """Convention for modded builds: keep the first 4 chars (game code + region)
       so the region still resolves for artwork, and replace the 2-char maker
       code.  SMNE01 -> SMNE99.  Keeping the region char means GameTDB art and
       save handling still behave."""
    return (base_id6[:4] + suffix[:2].zfill(2)).upper()


def place_rom(rom, cfg, dry_run=False, covers=None, force_system=None,
              custom_id=None, custom_title=None, cover_id=None, mod_suffix=None,
              force=False):
    """Route one ROM to the right home: Nintendo disc -> USB Loader GX,
       cartridge ROM -> emulator directory."""
    rom = Path(rom)
    ext = rom.suffix.lower()

    # explicit non-disc system forces the emulator path
    if force_system and force_system.lower() not in ("wii", "gamecube", "gc"):
        install_emu(rom, force_system, cfg, dry_run=dry_run, force=force)
        return

    if ext in DISC_EXTS or is_nkit(rom):
        id6, kind = read_disc_info(rom, cfg)
        if not id6:
            id6 = guess_id_from_name(rom.stem, install._titles)
        # --mod derives the custom id from the disc's own id (SMNE01 -> SMNE99)
        # and borrows the base game's artwork unless told otherwise
        if mod_suffix and not custom_id:
            if not id6:
                warn(f"--mod needs the base disc id, which isn't readable from "
                     f"{rom.name}; pass --id explicitly instead.")
                return
            custom_id = derive_mod_id(id6, mod_suffix)
            cover_id = cover_id or id6
            info(f"--mod: {id6} -> {custom_id} (art from {id6})")

        # a custom build may be unidentifiable — that's fine, the given id wins
        if not id6 and not custom_id:
            warn(f"skip (no game ID): {rom.name} — pass --id to set one yourself")
            return
        if kind == "unknown":
            kind = "gc" if ext == ".gcm" or (force_system or "").lower() in ("gc", "gamecube") else "wii"
        install(rom, id6, kind, cfg, dry_run=dry_run, covers=covers,
                custom_id=custom_id, custom_title=custom_title, cover_id=cover_id,
                force=force)
        return

    system = force_system or EMU_EXT_SYSTEM.get(ext)
    if system:
        install_emu(rom, system, cfg, dry_run=dry_run, force=force)
        return
    warn(f"unknown ROM type '{ext}': {rom.name} (use -s SYSTEM to force)")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

REGION_ALIASES = {
    "US": "usa", "USA": "usa", "NTSC": "usa", "AMERICA": "usa", "NA": "usa",
    "EU": "europe", "EUR": "europe", "PAL": "europe", "EUROPE": "europe",
    "UK": "europe", "GB": "europe",
    "JP": "japan", "JPN": "japan", "JAP": "japan", "JAPAN": "japan",
    "KR": "korea", "KO": "korea", "KOR": "korea", "KOREA": "korea",
    "AU": "australia", "AUS": "australia", "AUSTRALIA": "australia",
}


def _region_token(region):
    return REGION_ALIASES.get((region or "").upper(), (region or "").lower())


def pick_result(results, query, assume_yes, region="US"):
    """Choose a search hit, preferring the requested region (default US). The
       same game in several regions is not ambiguity — we just pick the region.
       Auto-picks when the intended title is clear; otherwise prompts."""
    if not results:
        return None
    want = _region_token(region)

    def rrank(r):
        reg = (r["region"] or "").lower()
        if want and want in reg:
            return 0                       # requested region first
        if "usa" in reg:
            return 1                       # US as a safe universal fallback
        return 2

    # rank by name similarity, then by region preference
    scored = sorted(results, key=lambda r: (-ratio(query, r["title"]), rrank(r)))
    top = scored[0]
    tr = ratio(query, top["title"])
    # a *different* game scoring nearly as high = real ambiguity (Mario Party vs 2)
    rival = next((r for r in scored[1:]
                  if normalize(r["title"]) != normalize(top["title"])), None)
    ambiguous = rival is not None and ratio(query, rival["title"]) > tr - 0.1

    if assume_yes or (tr >= 0.8 and not ambiguous):
        note = "" if want in (top["region"] or "").lower() else \
               f"  (no {region} copy — using closest region)"
        info(f"matched: {top['title']} [{top['region'] or '?'}] -> vault {top['vault']}{note}")
        return top

    print("  Multiple matches (region-preferred first):")
    for i, r in enumerate(scored[:12], 1):
        print(f"   {i:2}. {r['title']}  [{r['region'] or '?'}]  (vault {r['vault']})")
    try:
        sel = input("  pick #: ").strip()
        return scored[int(sel) - 1]
    except (ValueError, IndexError, EOFError):
        return None


def resolve_target(target, system):
    """Return a vault id from a name, numeric id, or full vimm URL."""
    m = re.search(r"vimm\.net/vault/(\d+)", target)
    if m:
        return m.group(1), None
    if target.isdigit():
        return target, None
    return None, target   # name -> needs search


def read_list_file(path):
    """One target per line; blank lines and '#' comments ignored."""
    out = []
    for line in Path(path).expanduser().read_text("utf-8", "replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _parse_entry(s, default_system):
    """A queue line may carry its own system, e.g. "GameCube: Wind Waker".
       Returns (target, system). URLs (https:) and normal names are unaffected."""
    m = re.match(r"^\s*(wii|gamecube|gc)\s*:\s*(.+)$", s, re.I)
    if m:
        sysmap = {"wii": "Wii", "gc": "GameCube", "gamecube": "GameCube"}
        return m.group(2).strip(), sysmap[m.group(1).lower()]
    return s, default_system


def expand_targets(tokens, files, default_system):
    """Flatten the queue into (target, system) pairs. Any token that is an
       existing (non-ROM) file, and any -f/--file, is read as a list, one per
       line. Each entry may prefix its own system (see _parse_entry)."""
    raw = []
    for t in tokens:
        p = Path(t).expanduser()
        if (p.is_file()
                and p.suffix.lower() not in ROM_ALL
                and p.suffix.lower() not in ARCHIVE_EXTS):
            raw += read_list_file(p)
        else:
            raw.append(t)
    for f in files:
        raw += read_list_file(f)
    return [_parse_entry(x, default_system) for x in raw]


# Vimm vaults often bundle a demo/kiosk/preview disc (its own game code) next to
# the real game. We drop those unless the request itself asked for one.
DEMO_RE = re.compile(r"\(\s*demo\s*\)|\bdemo\b|\bkiosk\b|taikenban|"
                     r"\btrial\s+(?:version|disc)\b|\bpreview\s+disc\b", re.I)


def is_demo_disc(title):
    return bool(title) and bool(DEMO_RE.search(str(title)))


def download_target(target, system, cfg, args):
    """Download half of the pipeline: resolve -> dedup/ledger skip -> claim ->
       download every disc. Returns either {"status": <terminal>} (nothing to
       install) or a job dict {vault_id, system, vkey, downloaded:[(archive,d)],
       n_discs, any_fail} that install_job() consumes and whose vault claim it
       releases."""
    region = getattr(args, "region", None) or cfg.get("region", "US")
    force = getattr(args, "force", False)

    vault_id, name = resolve_target(target, system)
    if vault_id is None:
        step(f"searching Vimm for '{name}' ({system}, prefer {region})")
        chosen = pick_result(vimm_search(name, system), name, args.yes, region)
        if not chosen:
            warn(f"no usable match for '{name}', skipping")
            return {"status": "not_found"}
        vault_id = chosen["vault"]

    if not force:                       # skip whole download if already installed
        entry = ledger_complete(cfg, vault_id)
        if entry:
            ids = ", ".join(sorted({d["id6"] for d in entry["discs"]}))
            ok(f"already installed, skipping vault {vault_id} "
               f"({entry.get('title', '?')} [{ids}]) — no download")
            if covers_flag(args) is not False:
                for d6 in {d["id6"] for d in entry["discs"]}:
                    download_covers(d6, cfg)
            return {"status": "skipped"}

    if not confirm(f"download & install vault {vault_id}?", args.yes):
        return {"status": "skipped"}

    vkey = f"{_drive_tag(cfg)}:vault:{vault_id}"
    if not force and not claim(vkey):
        ok(f"another wiivault run is handling vault {vault_id} — skipping")
        return {"status": "skipped"}

    # we now own vkey; release it on any early return, else install_job does
    discs, dl_url = find_media(vault_id, system)

    # drop demo/kiosk/preview discs the vault bundles with the real game, unless
    # the request asked for one (name/target says demo, or --demos / --disc)
    if not (getattr(args, "demos", False) or getattr(args, "disc", None)
            or is_demo_disc(target) or is_demo_disc(name)):
        real = [d for d in discs if not is_demo_disc(d.get("title", ""))]
        dropped = [d for d in discs if is_demo_disc(d.get("title", ""))]
        if dropped and real:
            info("skipping demo disc(s): "
                 + ", ".join(d["title"] for d in dropped) + "  (use --demos to keep)")
            discs = real
        elif dropped and not real:
            warn(f"vault {vault_id}: only demo/preview disc(s) — use --demos to install")
            release(vkey)
            return {"status": "skipped"}

    n_discs = len(discs)                                # after the demo filter
    if getattr(args, "disc", None):                     # --disc N picks just one
        discs = [d for d in discs if d["sort"] == args.disc]
        if not discs:
            warn(f"vault {vault_id} has no disc {args.disc}; skipping")
            release(vkey)
            return {"status": "failed"}
    if len(discs) > 1:
        step(f"{len(discs)} disc(s) on vault {vault_id}:")
        for d in discs:
            info(f"  {d['sort']}. {d['title'] or '(disc %d)' % d['sort']}  {d['size']}")

    downloaded, any_fail = [], False
    for d in discs:
        archive, _ = vimm_download(vault_id, system, cfg, alt=args.alt,
                                   host_override=args.host, media_id=d["id"], url=dl_url)
        if archive is None:
            warn(f"disc {d['sort']} of vault {vault_id} failed to download")
            any_fail = True
            continue
        downloaded.append((archive, d))
    if not downloaded:
        release(vkey)
        return {"status": "failed" if any_fail else "skipped"}
    return {"vault_id": vault_id, "system": system, "vkey": vkey,
            "downloaded": downloaded, "n_discs": n_discs, "any_fail": any_fail}


def install_job(job, cfg, args):
    """Install half: extract + convert + write each downloaded disc, then release
       the vault claim. Returns 'installed' | 'failed' | 'skipped'."""
    vault_id, system = job["vault_id"], job["system"]
    n_discs, force = job["n_discs"], getattr(args, "force", False)
    try:
        seen, any_ok, any_fail = {}, False, job.get("any_fail", False)
        for archive, d in job["downloaded"]:
            roms = unpack(archive, cfg)
            if not roms:
                warn(f"no disc image in {archive.name}; skipping"); any_fail = True; continue
            rom = max(roms, key=lambda p: p.stat().st_size)
            id6, kind = read_disc_info(rom, cfg)
            if not id6:
                id6 = guess_id_from_name(rom.stem, install._titles)
                kind = "wii" if system == "Wii" else "gc"
            if not id6:
                warn(f"no ID for {rom.name}; left in {rom.parent}"); any_fail = True; continue
            if kind == "unknown":
                kind = "wii" if system == "Wii" else "gc"
            # size-aware space guard: don't start a write that would blow past the
            # reserve. Output estimate: ISO/WBFS shrink or match; CISO expands to a
            # full GameCube ISO (~1.36 GiB).
            min_free = getattr(args, "min_free", 0) or 0
            if min_free and not args.dry_run and not is_installed(id6, kind, cfg, seen.get(id6, 0) + 1):
                out_gib = rom.stat().st_size / 1024**3
                if kind == "gc" and rom.suffix.lower() == ".ciso":
                    out_gib = max(out_gib, 1.4)
                if free_gib(cfg) < out_gib + min_free:
                    warn(f"{id6} needs ~{out_gib:.1f} GiB + {min_free} reserve but "
                         f"only {free_gib(cfg):.1f} GiB free — deferring (stays pending)")
                    return "deferred"                    # keep cache, retry later
            seen[id6] = seen.get(id6, 0) + 1
            result = install(rom, id6, kind, cfg, dry_run=args.dry_run,
                             covers=covers_flag(args), disc_no=seen[id6], force=force)
            if result is not None:
                any_ok = True
                if not args.dry_run:                    # record every disc
                    ledger_add_disc(cfg, vault_id, system,
                                    title_for(id6, install._titles) or "",
                                    id6, kind, seen[id6], n_discs)
                    if not getattr(args, "keep_download", False):
                        cleanup_download(archive, cfg)   # one copy: on the drive
            else:
                any_fail = True                          # keep cache for resume
        if any_ok:
            return "installed"
        return "failed" if any_fail else "skipped"
    finally:
        release(job["vkey"])


def process_target(target, system, cfg, args):
    """Sequential download-then-install of one target. Returns a status string."""
    job = download_target(target, system, cfg, args)
    if "status" in job:
        return job["status"]
    return install_job(job, cfg, args)


def run_pipeline(items, cfg, args, on_status):
    """Overlap download and install: one thread downloads Vimm serially (the host
       allows one at a time) while another installs the previous game. `items` is
       a list of (target, system, ref); on_status(ref, status) fires as each
       finishes. Falls back to sequential when args.parallel is not set."""
    min_free = getattr(args, "min_free", 0) or 0
    parallel = getattr(args, "parallel", False)

    if not parallel:
        for target, system, ref in items:
            if min_free and free_gib(cfg) < min_free:
                warn(f"free space below {min_free} GiB — stopping"); break
            status = process_target(target, system, cfg, args)
            on_status(ref, status)
            if status == "deferred":            # didn't fit — stop, keep it pending
                break
        return

    global QUIET
    import queue as _queue
    handoff = _queue.Queue(maxsize=1)       # ~1 game downloaded ahead of install
    stop = threading.Event()
    lock = threading.Lock()

    def downloader():
        try:
            for target, system, ref in items:
                if stop.is_set():
                    break
                with lock:
                    step(f"[dl] {target} ({system})")
                res = download_target(target, system, cfg, args)
                if "status" in res:
                    with lock:
                        on_status(ref, res["status"])
                else:
                    res["ref"] = ref
                    handoff.put(res)         # blocks while installer is busy
        finally:
            handoff.put(None)                # sentinel

    def installer():
        while True:
            job = handoff.get()
            if job is None:
                break
            ref = job["ref"]
            if stop.is_set():                # draining after a stop
                release(job["vkey"])
                with lock:
                    on_status(ref, "deferred")   # not done — retry later
                continue
            if min_free and free_gib(cfg) < min_free:
                release(job["vkey"])
                with lock:
                    warn(f"free space below {min_free} GiB — stopping "
                         f"(this game and the rest stay pending)")
                    on_status(ref, "deferred")
                stop.set()
                continue
            with lock:
                step(f"[install] vault {job['vault_id']} ({job['system']})")
            status = install_job(job, cfg, args)
            if status == "deferred":            # didn't fit — stop taking more
                stop.set()
            with lock:
                on_status(ref, status)

    QUIET = True
    try:
        dl = threading.Thread(target=downloader)
        io = threading.Thread(target=installer)
        dl.start(); io.start(); dl.join(); io.join()
    finally:
        QUIET = False


def cmd_get(args):
    cfg = apply_out(load_config(), args.out)
    install._titles = ensure_titles(cfg)
    targets = expand_targets(args.targets, args.file, args.system)
    if not targets:
        die("nothing to install (no names/ids given and no list file had entries)")
    region = args.region or cfg.get("region", "US")
    step(f"queue: {len(targets)} item(s), region {region}"
         + ("  (parallel)" if getattr(args, "parallel", False) else ""))
    run_pipeline([(t, s, None) for (t, s) in targets], cfg, args, lambda ref, st: None)


def guess_id_from_name(stem, titles):
    """Last-resort filename match. Deliberately strict: a loose match once filed
       a US retail disc under a Japanese demo's ID (wrong folder AND wrong art),
       so we'd rather return nothing than guess."""
    best, best_r = None, 0.0
    for gid, name in titles.items():
        r = ratio(stem, name)
        if r > best_r:
            best, best_r = gid, r
    if best_r < 0.92:
        return None
    warn(f"no disc ID readable — matched by filename only ({best_r:.2f}): "
         f"{titles.get(best)} [{best}]. Verify this is right.")
    return best


def cmd_search(args):
    cfg = load_config()
    hits = vimm_search(" ".join(args.query), args.system)
    if not hits:
        warn("no results")
        return
    q = " ".join(args.query)
    for r in sorted(hits, key=lambda r: ratio(q, r["title"]), reverse=True):
        print(f"  vault {r['vault']:>7}  {r['title']}  [{r['region'] or '?'}]")


def cmd_organize(args):
    cfg = apply_out(load_config(), args.out)
    install._titles = ensure_titles(cfg)
    if not args.paths and not args.dirs:
        die("give a ROM/archive file or a folder (positional), or -d FOLDER")

    custom_id = args.custom_id
    for label, val in (("--id", custom_id), ("--cover-id", args.cover_id)):
        if val and not re.fullmatch(r"[A-Za-z0-9]{6}", val):
            die(f"{label} must be exactly 6 letters/digits (got '{val}')")
    if custom_id:
        custom_id = custom_id.upper()
    if args.mod_suffix:
        if not re.fullmatch(r"[A-Za-z0-9]{1,2}", args.mod_suffix):
            die(f"--mod suffix must be 1-2 letters/digits (got '{args.mod_suffix}')")
        if custom_id:
            die("use either --mod (derive the id) or --id (set it), not both")
    if (args.title or args.cover_id) and not (custom_id or args.mod_suffix):
        warn("--title/--cover-id are only applied together with --id or --mod")

    roms = gather(args.paths, args.dirs, cfg)
    if not roms:
        warn("nothing to import (no ROMs or archives found)")
        return
    if custom_id and len(roms) > 1:
        die(f"--id sets one specific game id, but {len(roms)} ROMs were found. "
            f"Import custom builds one at a time.")
    step(f"importing {len(roms)} ROM(s)")
    for rom in roms:
        place_rom(rom, cfg, dry_run=args.dry_run,
                  covers=covers_flag(args), force_system=args.system,
                  custom_id=custom_id, custom_title=args.title,
                  cover_id=args.cover_id, mod_suffix=args.mod_suffix,
                  force=args.force)


def cmd_covers(args):
    cfg = load_config()
    ids = set()
    for root in (Path(cfg["wii_dir"]) / "wbfs", Path(cfg["gc_dir"]) / "games"):
        if root.exists():
            for d in root.iterdir():
                m = re.search(r"\[([A-Z0-9]{6})\]", d.name)
                if m:
                    ids.add(m.group(1))
    if not ids:
        warn("no installed games found under wbfs/ or games/")
        return
    step(f"fetching art for {len(ids)} game(s) -> {cfg['covers_dir']}")
    for i in sorted(ids):
        download_covers(i, cfg)


# --------------------------------------------------------------------------- #
# persistent queue  (CONFIG_DIR/queue.json — global, independent of the drive)
# --------------------------------------------------------------------------- #

QUEUE_FILE = CONFIG_DIR / "queue.json"


def load_queue():
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except (ValueError, OSError):
            warn("queue file was unreadable; starting a fresh queue")
    return []


def save_queue(q):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(q, indent=2))


def _queue_add_entries(q, raw_entries, default_system):
    """Append parsed entries, skipping ones already queued (normalized compare)."""
    have = {(normalize(e["line"]), e["system"]) for e in q}
    added = 0
    for s in raw_entries:
        line, system = _parse_entry(s, default_system)
        key = (normalize(line), system)
        if key in have:
            info(f"already queued: {line} ({system})")
            continue
        q.append({"line": line, "system": system, "status": "pending",
                  "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "note": ""})
        have.add(key)
        added += 1
    return added


def cmd_queue(args):
    q = load_queue()

    if args.action == "add":
        raw = list(args.entries)
        for f in args.file:
            if f == "-":
                raw += [ln.strip() for ln in sys.stdin.read().splitlines()
                        if ln.strip() and not ln.strip().startswith("#")]
            else:
                raw += read_list_file(f)
        if not raw:
            die("nothing to add (give entries, -f FILE, or - for stdin)")
        n = _queue_add_entries(q, raw, args.system)
        save_queue(q)
        ok(f"added {n} entr{'y' if n == 1 else 'ies'}; queue now has {len(q)}")

    elif args.action == "remove":
        removed = 0
        if args.index:
            for i in sorted(args.index, reverse=True):
                if 1 <= i <= len(q):
                    info(f"removing #{i}: {q[i-1]['line']}")
                    del q[i - 1]; removed += 1
                else:
                    warn(f"no entry #{i}")
        for term in args.entries:
            keep = []
            for e in q:
                if ratio(term, e["line"]) > 0.8 or normalize(term) == normalize(e["line"]):
                    info(f"removing: {e['line']} ({e['system']})"); removed += 1
                else:
                    keep.append(e)
            q = keep
        save_queue(q)
        ok(f"removed {removed}; queue now has {len(q)}")

    elif args.action == "clear":
        if not q:
            info("queue is already empty"); return
        if not confirm(f"clear all {len(q)} queued entr{'y' if len(q)==1 else 'ies'}?",
                       args.yes):
            return
        save_queue([])
        ok("queue cleared")

    elif args.action == "list":
        if not q:
            info("queue is empty"); return
        if args.format == "text":
            for e in q:
                prefix = "" if e["system"] == "Wii" else f"{e['system']}: "
                print(f"{prefix}{e['line']}")
        else:
            counts = {}
            for i, e in enumerate(q, 1):
                counts[e["status"]] = counts.get(e["status"], 0) + 1
                mark = {"pending": " ", "installed": "✓", "failed": "✗",
                        "skipped": "-"}.get(e["status"], "?")
                print(f"  {i:2}. [{mark}] {e['line']}  ({e['system']}, {e['status']})")
            print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    elif args.action == "run":
        cfg = apply_out(load_config(), args.out)
        install._titles = ensure_titles(cfg)
        pending = [e for e in q if e["status"] in ("pending", "failed")]
        if not pending:
            info("nothing pending in the queue"); return
        region = args.region or cfg.get("region", "US")
        step(f"draining queue: {len(pending)} pending, region {region}"
             + ("  (parallel)" if getattr(args, "parallel", False) else ""))

        def mark(ref, status):
            if args.dry_run:
                return
            ref["status"] = {"installed": "installed", "skipped": "skipped",
                             "not_found": "failed", "failed": "failed",
                             "deferred": "pending"}[status]   # deferred = retry later
            save_queue(q)                              # persist after each = resume

        run_pipeline([(e["line"], e["system"], e) for e in pending], cfg, args, mark)
        done = [e for e in q if e["status"] == "installed"]
        if not args.keep_installed and not args.dry_run:
            q = [e for e in q if e["status"] != "installed"]
            save_queue(q)
        ok(f"queue run complete: {len(done)} installed, {len(q)} remain"
           + (" (installed kept)" if args.keep_installed else ""))


def cmd_config(args):
    cfg = load_config()
    changed = False
    for key in ("wii_dir", "gc_dir", "covers_dir", "emu_dir", "download_dir",
                "region", "lang", "wit", "nkit", "split_size"):
        val = getattr(args, key, None)
        if val is not None:
            cfg[key] = val
            changed = True
    for flag, key, value in (("no_convert", "convert_to_wbfs", False),
                             ("convert", "convert_to_wbfs", True),
                             ("no_covers", "download_covers", False),
                             ("covers", "download_covers", True)):
        if getattr(args, flag, False):
            cfg[key] = value
            changed = True
    if changed:
        save_config(cfg)
        ok(f"saved {CONFIG_FILE}")
    print(json.dumps(cfg, indent=2))


def cmd_update_db(args):
    cfg = load_config()
    titles = ensure_titles(cfg, force=True)
    ok(f"cached {len(titles)} titles at {TITLES_CACHE}")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="wiivault",
        description="Queue Vimm ROMs, name them via GameTDB, and lay them out "
                    "for USB Loader GX.")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", aliases=["install"],
                       help="download + install one or more games (the queue)")
    g.add_argument("targets", nargs="*",
                   help='game name(s) (quoted, close is fine), vault id(s), '
                        'vimm URL(s), or a path to a list file (one per line)')
    g.add_argument("-f", "--file", action="append", default=[], metavar="LIST",
                   help="file with one name/id/url per line (repeatable)")
    g.add_argument("-s", "--system", default="Wii",
                   help="Vimm system (Wii, GameCube, ...); default Wii. "
                        "Per-entry override in a list: 'GameCube: <name>'")
    g.add_argument("-r", "--region", default=None,
                   help="preferred region: US (default), EU, JP, KO, AU")
    g.add_argument("--disc", type=int, default=None, metavar="N",
                   help="only fetch disc N of a multi-disc entry (default: all)")
    g.add_argument("--alt", default="0", help="Vimm alternate-format id (default 0)")
    g.add_argument("--host", help="override download host, e.g. download3.vimm.net")
    g.add_argument("-y", "--yes", action="store_true", help="no prompts; auto-pick best match")
    g.add_argument("-n", "--dry-run", action="store_true", help="show placement, don't write")
    g.add_argument("-o", "--out", metavar="DIR",
                   help="output drive root for this run (overrides config)")
    g.add_argument("--covers", action="store_true", help="download cover/disc art (on by default)")
    g.add_argument("--no-covers", action="store_true", help="skip cover/disc art for this run")
    g.add_argument("--force", action="store_true",
                   help="re-download/re-copy even if already installed")
    g.add_argument("--keep-download", action="store_true",
                   help="keep the cached archive after install (default: delete it)")
    g.add_argument("--parallel", action="store_true",
                   help="convert/write each game while the next one downloads")
    g.add_argument("--min-free", type=float, default=1.0, metavar="GIB",
                   help="stop before installing if drive free space is below this (GiB)")
    g.add_argument("--demos", action="store_true",
                   help="also install demo/kiosk/preview discs a vault bundles")
    g.set_defaults(func=cmd_get)

    s = sub.add_parser("search", help="search Vimm without downloading")
    s.add_argument("query", nargs="+")
    s.add_argument("-s", "--system", default="Wii")
    s.set_defaults(func=cmd_search)

    o = sub.add_parser("organize", aliases=["import"],
                       help="import ROMs/archives (files or folders) into the right layout")
    o.add_argument("paths", nargs="*",
                   help="ROM or archive file(s), and/or folder(s) to scan")
    o.add_argument("-d", "--dir", action="append", default=[], dest="dirs",
                   metavar="FOLDER", help="folder to scan (repeatable)")
    o.add_argument("-s", "--system",
                   help="force system for retro/ambiguous files, e.g. SNES, N64")
    o.add_argument("--id", dest="custom_id", metavar="ID6",
                   help="custom 6-char game id for a modded build; patched into "
                        "the disc so the loader sees it as its own game")
    o.add_argument("--title", metavar="NAME",
                   help="custom title (folder name + patched disc title)")
    o.add_argument("--cover-id", dest="cover_id", metavar="ID6",
                   help="fetch GameTDB art using this id (e.g. the base game) "
                        "but file it under --id")
    o.add_argument("--mod", dest="mod_suffix", nargs="?", const="99",
                   metavar="NN",
                   help="modded build: derive the id from the disc itself by "
                        "replacing the maker code (SMNE01 -> SMNE99) and reuse "
                        "the base game's art. Optional 2-char suffix, default 99")
    o.add_argument("-n", "--dry-run", action="store_true")
    o.add_argument("-o", "--out", metavar="DIR",
                   help="output drive root for this run (overrides config)")
    o.add_argument("--covers", action="store_true", help="download cover/disc art (off by default)")
    o.add_argument("--no-covers", action="store_true", help="skip cover/disc art (default)")
    o.add_argument("--force", action="store_true",
                   help="re-copy even if already installed")
    o.set_defaults(func=cmd_organize)

    cv = sub.add_parser("covers",
                        help="download missing cover/disc art for the whole library")
    cv.set_defaults(func=cmd_covers)

    c = sub.add_parser("config", help="set/show remembered directories")
    c.add_argument("--wii-dir", dest="wii_dir", help="root containing wbfs/")
    c.add_argument("--gc-dir", dest="gc_dir", help="root containing games/")
    c.add_argument("--covers-dir", dest="covers_dir", help="root containing 2D/3D/Full/Disc/")
    c.add_argument("--emu-dir", dest="emu_dir",
                   help="drive root for retro ROMs (each emulator has its own subfolder)")
    c.add_argument("--download-dir", dest="download_dir")
    c.add_argument("--region", help="default preferred region (US, EU, JP, KO, AU)")
    c.add_argument("--lang", help="GameTDB language (EN, JA, ...)")
    c.add_argument("--wit", help="path to Wiimms ISO Tools 'wit' binary")
    c.add_argument("--nkit", help="path to Nanook/NKit binary (restores .nkit.iso)")
    c.add_argument("--split-size", dest="split_size", help="e.g. 4gb")
    c.add_argument("--no-convert", action="store_true", help="don't convert iso->wbfs")
    c.add_argument("--convert", action="store_true", help="convert iso->wbfs (default)")
    c.add_argument("--no-covers", action="store_true", help="don't auto-download art")
    c.add_argument("--covers", action="store_true", help="auto-download art (default)")
    c.set_defaults(func=cmd_config)

    u = sub.add_parser("update-db", help="refresh the GameTDB title cache")
    u.set_defaults(func=cmd_update_db)

    # ---- queue: build a batch across invocations, then run it ----
    q = sub.add_parser("queue", help="build a persistent install queue, then run it")
    qsub = q.add_subparsers(dest="action", required=True)

    qa = qsub.add_parser("add", help="append names / ids / urls / 'System: Name'")
    qa.add_argument("entries", nargs="*")
    qa.add_argument("-f", "--file", action="append", default=[], metavar="LIST",
                    help="append every line of LIST ('-' for stdin; repeatable)")
    qa.add_argument("-s", "--system", default="Wii", help="default system (Wii)")

    qr = qsub.add_parser("remove", help="remove by name/id (fuzzy) or index")
    qr.add_argument("entries", nargs="*")
    qr.add_argument("-i", "--index", type=int, action="append", default=[],
                    metavar="N", help="remove the 1-based entry N from `list`")

    ql = qsub.add_parser("list", help="show the queue")
    ql.add_argument("--format", choices=["table", "text"], default="table",
                    help="'text' dumps re-addable lines")

    qc = qsub.add_parser("clear", help="empty the queue")
    qc.add_argument("-y", "--yes", action="store_true")

    qrun = qsub.add_parser("run", help="download+install all pending entries")
    qrun.add_argument("-r", "--region", default=None)
    qrun.add_argument("--alt", default="0")
    qrun.add_argument("--host")
    qrun.add_argument("--disc", type=int, default=None)
    qrun.add_argument("-y", "--yes", action="store_true")
    qrun.add_argument("-n", "--dry-run", action="store_true")
    qrun.add_argument("-o", "--out", metavar="DIR")
    qrun.add_argument("--covers", action="store_true")
    qrun.add_argument("--no-covers", action="store_true")
    qrun.add_argument("--force", action="store_true")
    qrun.add_argument("--keep-download", action="store_true",
                      help="keep cached archives after install (default: delete)")
    qrun.add_argument("--parallel", action="store_true",
                      help="convert/write each game while the next one downloads")
    qrun.add_argument("--min-free", type=float, default=1.0, metavar="GIB",
                      help="stop before installing if free space drops below this (GiB)")
    qrun.add_argument("--demos", action="store_true",
                      help="also install demo/kiosk/preview discs a vault bundles")
    qrun.add_argument("--keep-installed", action="store_true",
                      help="mark installed entries instead of dropping them")

    q.set_defaults(func=cmd_queue)
    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print()
        die("interrupted")


if __name__ == "__main__":
    main()
