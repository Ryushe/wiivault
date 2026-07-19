# wiivault

Queue Wii/GameCube ROMs from **Vimm's Lair**, name them from **GameTDB**, convert
them to the format that boots, download **cover + disc art** (like Wii Backup
Manager), and drop everything into a **USB Loader GX** tree automatically. Local
files, folders, and archives (zip/7z/rar) are handled too, and retro cartridge
ROMs are sorted into an emulator directory.

```
get:     search Vimm → download → extract → (de-NKit) → read disc ID6 →
         look up title → convert to wbfs / game.iso → place → download art
import:  file / folder / archive → classify each ROM → route to the right home
```

Pure Python 3 standard library — no `pip install` required. Only download games
you own.

---

## Install & setup

Just the one script, `wiivault.py`. Run with `python3 wiivault.py <command>`.

Defaults are already set for the Windows drive **H:\ → `/mnt/h`** (WSL). The
"drive root" is the folder that *contains* `wbfs/`, `games/`, and `covers/`.
Settings live in `~/.config/wiivault/config.json`.

```bash
# only needed if your drive isn't /mnt/h:
python3 wiivault.py config --wii-dir /mnt/h --gc-dir /mnt/h \
                           --covers-dir /mnt/h/covers --emu-dir /mnt/h/roms
```

## Requirements

**Python:** 3.8+ — **standard library only, nothing to `pip install`.**

**System tools:** these do the unpacking and disc-image conversion. Install them
all in one go:

```bash
./install-deps.sh
# or manually:
sudo apt-get install -y python3 p7zip-full wit unrar
```

| Tool | apt package | Used for | If missing |
|------|-------------|----------|------------|
| **7-Zip** (`7z`) | `p7zip-full` | unpacks the **`.7z` archives Vimm ships** | those archives are skipped (`.zip` works without it — built into Python) |
| **Wiimms ISO Tools** (`wit`) | `wit` | **CISO/ISO → `game.iso` or `.wbfs`**, and 4 GB FAT32 splitting (`.wbf1`) | disc copied raw with a warning — **GameCube games won't boot** (Vimm ships CISO) |
| **unrar** | `unrar` | `.rar` archives | `.rar` skipped (rare; 7z covers most) |
| **Nanook/NKit** (`nkit`) | *not in apt* | restore `.nkit.iso` → full ISO | NKit files detected and **skipped** (never written broken) |

`wit` is effectively **required for GameCube**: Vimm offers GameCube only as
`.ciso`, `.nkit.iso`, or `.rvz` — there is no plain-ISO option — and USB Loader GX
wants a plain `game.iso`.

NKit isn't packaged; it needs the .NET 6 runtime. Grab it from
[Nanook/NKit](https://github.com/Nanook/NKit), then:
`python3 wiivault.py config --nkit /path/to/nkit`

All are auto-detected on `PATH`; override a location with
`config --wit <path>` / `config --nkit <path>`.

### Drive format: use FAT32

Format the USB drive as **FAT32**, ideally with a **32 KB cluster size**.

| Filesystem | Wii (USB Loader GX) | GameCube (Nintendont) |
|------------|--------------------|-----------------------|
| **FAT32**  | works              | **works** — recommended |
| exFAT      | works              | works                 |
| NTFS       | works              | **does NOT work**     |

Nintendont — what USB Loader GX uses to launch GameCube games — has no NTFS
support. An NTFS drive will happily run your Wii games and then silently fail to
launch anything GameCube, so FAT32 is the safe single-partition choice. Some
GameCube loaders also require the games to be on the **first partition**.

**FAT32's catch: no file may exceed 4 GiB.** Wii discs can be 4.7 GB (8.5 GB
dual-layer), so large Wii games are split into `ID6.wbfs` + `ID6.wbf1` (+`.wbf2`…),
which the loader reassembles transparently. That's what the `split_size` setting
controls; the default `4095M` sits just under the limit. GameCube images top out
around 1.36 GB, so they are never split — Nintendont needs a single `game.iso`.

> If you ever switch to exFAT/NTFS the splitting is unnecessary but harmless.
> Don't use NTFS if you want GameCube games.

### Cover art

Art lives in a `covers/` folder at the drive root, keeping USB Loader GX's
folder convention (3D covers in the root, the rest in subfolders):

```
<drive>/covers/<ID6>.png        3D covers  (the root)
<drive>/covers/2D/<ID6>.png     2D flat covers
<drive>/covers/full/<ID6>.png   full box covers
<drive>/covers/disc/<ID6>.png   disc labels
```

Covers download automatically with every game. Move it with
`config --covers-dir <path>`.

**One-time Wii setup** — Settings → Global Settings → Custom Paths:

| Setting | Path |
|---------|------|
| 3D Covers | `usb1:/covers` |
| 2D Covers | `usb1:/covers/2D` |
| Full Covers | `usb1:/covers/full` |
| Disc Art | `usb1:/covers/disc` |

The animated banner + music you see when highlighting a game is **not** a
download — the loader reads it out of the game file itself.

---

## Formats — where each thing lands

| System | Folder | File | Named from |
|--------|--------|------|-----------|
| Wii | `<wii_dir>/wbfs/` | `Title [ID6]/ID6.wbfs` | GameTDB |
| GameCube | `<gc_dir>/games/` | `Title [ID6]/game.iso` | GameTDB |
| Covers / disc art | `<covers_dir>/` | `2D\|3D\|Full\|Disc/ID6.png` | GameTDB art |
| Retro (SNES, N64, …) | `<emu_dir>/<System>/` | original filename | as-is |

> **GameCube is `game.iso`, NOT wbfs.** WBFS is a Wii-only container; Nintendont
> (what USB Loader GX uses for GameCube) won't launch a GC game stored as wbfs.
> So Wii→`wbfs/*.wbfs`, GameCube→`games/game.iso`. Naming/IDs/covers are shared
> between the two — only the container/folder differs.

### How routing is decided

Every file — passed directly, found in a folder, or pulled out of an archive —
is classified automatically:

- **Nintendo disc image** (`.iso .wbfs .gcm .rvz .wia .ciso .gcz`, incl. NKit):
  the ID6 and Wii-vs-GameCube are read straight from the **disc header** (magic
  words `0x5D1C9EA3` = Wii, `0xC2339F3D` = GameCube), so messy filenames don't
  matter. Goes to the USB Loader GX tree with the canonical GameTDB name.
- **Cartridge ROM** (`.sfc .nes .n64/.z64 .gb/.gbc/.gba .md .sms .pce` …):
  copied to `<emu_dir>/<System>/`, **filename kept** (retro loaders match by
  filename/DAT, not a disc ID — GameTDB IDs are Nintendo-disc-only).
- **Ambiguous** (e.g. a bare `.bin`): pass `-s SYSTEM` to force it.

---

## Command reference

### `get` (alias `install`) — download + install (the queue)
```bash
python3 wiivault.py install "Mario Kart Wii" "Wii Sports" 63650   # quoted names / ids / urls
python3 wiivault.py install queue.txt                             # a list file (one per line)
python3 wiivault.py install -f queue.txt -y                       # same, unattended
```
Names, numeric vault IDs, and full `vimm.net/vault/NNNN` URLs can be mixed and are
processed in order. **Names don't have to be exact** — special characters are
stripped (`Zelda: Wind Waker!!!` → finds `The Legend of Zelda: The Wind Waker`)
and the closest match is chosen (fuzzy). When a name matches the same game in
several regions, it **defaults to the US copy** (override with `-r`). A **list
file** is any text file with one entry per line; blank lines and `#` comments are
ignored. It's used automatically if you pass its path as an argument, or with `-f`.

| Flag | Meaning |
|------|---------|
| `-f, --file LIST` | text file with one name/id/url per line (repeatable) |
| `-s, --system` | Vimm system: `Wii` (default), `GameCube`, … |
| `-r, --region` | preferred region: `US` (default), `EU`, `JP`, `KO`, `AU` |
| `-y, --yes` | no prompts; auto-pick the best fuzzy match (good for big queues) |
| `-n, --dry-run` | show where each game would land, write nothing |
| `-o, --out DIR` | output drive root for this run (overrides config) |
| `--alt N` | Vimm alternate-format id (default `0`) |
| `--host H` | override download host, e.g. `download3.vimm.net` |
| `--covers` | download cover/disc art (on by default) |
| `--no-covers` | skip cover/disc art for this run |
| `--force` | re-download / re-copy even if already installed |

**One copy at finish.** A downloaded game passes through the cache
(`~/.cache/wiivault/downloads/`) as a `.7z` and an extraction, then is copied to
the drive. On a **successful** install the cached archive + extraction are
**deleted**, so the game ends up in exactly one place (the drive). On failure
they're kept so a re-run resumes without re-downloading. Pass `--keep-download`
to retain the archive. (`import` never deletes your own source files.)

**Already-installed games are skipped.** Before copying, wiivault checks the
drive for a `* [ID6]` folder with the game already in it; if found, it skips the
copy (and, for `get`, skips the **download** too when its library ledger proves
it's there). Covers are still backfilled on a skip. Use `--force` to override —
e.g. to replace a corrupt file or re-patch a `--mod` build. The filesystem is
authoritative: delete a game by hand and it re-installs.

**Region:** falls back to the closest available region (with a note) if your
preferred one doesn't exist for that game. Set a different default permanently
with `config --region EU`.

**Per-entry system:** the run-wide `-s` sets the system, but a single list entry
can override it by prefixing `Wii:` or `GameCube:` — so one queue can hold both.

Example `queue.txt`:
```
# my install queue           (system defaults to -s / Wii)
Mario Kart Wii
Wii Sports
GameCube: The Wind Waker      # this one is fetched as GameCube
GameCube: Super Smash Bros Melee
```

### `import` (alias `organize`) — local files / folders / archives
```bash
python3 wiivault.py import [path…] [-d FOLDER]… [flags]
```
| Flag | Meaning |
|------|---------|
| `-d, --dir FOLDER` | folder to scan (repeatable); same effect as a folder positional |
| `-s, --system` | force system for retro/ambiguous files (`SNES`, `N64`, …) |
| `-n, --dry-run` | preview placement only |
| `-o, --out DIR` | output drive root for this run (overrides config) |
| `--covers` | download cover/disc art (on by default) |
| `--no-covers` | skip cover/disc art for this run |
| `--force` | re-copy even if already installed |

### `queue` — build a batch across sessions, then run it

A persistent queue at `~/.config/wiivault/queue.json`, so you can add games over
time and install them all later. Entries are names / vault ids / vimm urls, with
the same `System: Name` prefix syntax as list files.

```bash
python3 wiivault.py queue add "Mario Kart Wii" "GameCube: Wind Waker"
python3 wiivault.py queue add -f mylist.txt        # append a list file ('-' = stdin)
python3 wiivault.py queue list                     # index, entry, system, status
python3 wiivault.py queue list --format text       # dump re-addable lines
python3 wiivault.py queue remove "Wind Waker"      # by fuzzy name…
python3 wiivault.py queue remove -i 2              # …or by index
python3 wiivault.py queue run -y                   # download+install all pending
python3 wiivault.py queue clear
```

`queue run` reuses the exact `get` pipeline and accepts its flags
(`-o -r -y -n --covers/--no-covers --force`). It **records status per entry**
(`pending → installed / failed / skipped`) and saves after each one, so an
interrupted run **resumes** by just re-running — installed entries are dropped
(or kept with `--keep-installed`), pending + failed are retried. `add` de-dupes
against what's already queued.

### `search` — Vimm search, no download
```bash
python3 wiivault.py search "mario kart" -s Wii
```

### `covers` — back-fill art for the whole installed library
```bash
python3 wiivault.py covers
```
Scans `wbfs/` and `games/`, reads each `[ID6]` from the folder names, downloads
any missing 2D/3D/Full/Disc art.

### `config` — view or change remembered settings
```bash
python3 wiivault.py config                       # print current config
python3 wiivault.py config --wii-dir /mnt/h …    # set values
```

### `update-db` — refresh the GameTDB title cache
```bash
python3 wiivault.py update-db
```

---

## Configuration keys (`~/.config/wiivault/config.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `wii_dir` | `/mnt/h` | root containing `wbfs/` |
| `gc_dir` | `/mnt/h` | root containing `games/` |
| `covers_dir` | `/mnt/h/covers` | root containing `2D/ 3D/ Full/ Disc/` |
| `emu_dir` | `/mnt/h/roms` | retro ROMs → `<emu_dir>/<System>/` |
| `download_dir` | `~/.cache/wiivault/downloads` | temp download + extract area |
| `region` | `US` | preferred region when a name matches several (`-r` overrides) |
| `lang` | `EN` | GameTDB language for titles + art fallback |
| `wit` | `wit` | path to Wiimms ISO Tools |
| `nkit` | `nkit` | path to Nanook/NKit |
| `convert_to_wbfs` | `true` | convert Wii ISO→WBFS when `wit` present |
| `download_covers` | `true` | auto-download box + disc art on every install |
| `split_size` | `4gb` | FAT32 split size for `wit` |

Config flags: `--no-convert`/`--convert`, `--no-covers`/`--covers`.

---

## How the tricky bits work (for reference)

- **Vimm download**: fetches the vault page, scrapes the real `mediaId` (which is
  **not** the vault number — e.g. Mario Kart USA is vault `17746`, mediaId
  `9281`), detects the `downloadN.vimm.net` host from the form action, POSTs
  `mediaId`+`alt`, and names the file from the server's `Content-Disposition`.
  Vimm rotates hosts and removes the download button periodically — use `--host`
  if a download 403s or returns a tiny file.
- **Titles**: from GameTDB `titles.txt` (`ID6 = Title`), cached locally.
- **Cover art**: `https://art.gametdb.com/wii/<type>/<region>/<ID6>.png` for both
  Wii *and* GameCube (GC art lives under the `wii` path). Region folder is chosen
  from the ID6 region char with fallbacks (E→US, P→EN→US, D→DE→EN→US, J→JA→US …).
- **NKit**: detected by `.nkit.` in the name or the `NKIT` magic in the header,
  restored with `nkit -task convert <in> <out.iso>` before conversion.

---

## Known limitations / not done yet

- **Retro ROMs are not renamed** — kept as-is on purpose (per user preference).
  No-Intro/DAT-based renaming for cartridge systems would be a separate feature.
- **Retro folder layout is a guess** — `<emu_dir>/<System>/` matches the common
  RetroArch / USB Loader GX plugin convention, but different loaders (WiiFlow,
  specific plugins) want different paths. Adjust `emu_dir`, or the `System`
  names in `EMU_EXT_SYSTEM`, to match your loader.
- **The live Vimm download + real `wit`/`nkit` conversion have not been run
  end-to-end here** (no `wit`/`nkit` on the dev box, and no real ROM pulled).
  Everything around them is tested; the actual multi-GB fetch + convert is the
  one path to confirm on the real machine.
- **Vimm scraping is fragile** by nature (mediaId/host/markup can change). The
  `search`, `find_media`, and download parsing may need small regex updates if
  Vimm changes their pages.
- **Disc-based retro systems** (PS1/Saturn `.bin/.cue/.iso`) aren't auto-classified
  — `.iso` is assumed Nintendo; use `-s` for those.

---

## Layout example

```
/mnt/h/wbfs/Mario Kart Wii [RMCE01]/RMCE01.wbfs
/mnt/h/games/The Legend of Zelda- The Wind Waker [GZLE01]/game.iso
/mnt/h/covers/{2D,3D,Full,Disc}/RMCE01.png
/mnt/h/roms/SNES/Super Mario World.sfc
```
