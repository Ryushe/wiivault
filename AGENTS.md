# AGENTS.md — wiivault

Pickup notes for an AI agent resuming work on `wiivault.py`. Read this first, then
`README.md` for user-facing usage. Everything is one file: `wiivault.py`
(pure Python 3 stdlib, no deps).

## What it is

A CLI that builds a **USB Loader GX** library for a Wii. It:
1. Downloads Wii/GameCube ROMs from **Vimm's Lair** (`get`), or imports local
   files/folders/archives (`import`/`organize`).
2. Identifies each disc by reading the **ID6 from the disc header**.
3. Names folders from **GameTDB** `titles.txt`.
4. Converts to the format that boots: Wii → `.wbfs`, GameCube → `game.iso`.
5. Downloads cover/disc art from GameTDB (`art.gametdb.com`).
6. Sorts non-Nintendo cartridge ROMs into `<emu_dir>/<System>/`.

User is **Ryushe**, on **WSL**, target drive **H:\ = `/mnt/h`**. Config lives in
`~/.config/wiivault/config.json`.

## Hard-won facts (verified — don't re-derive or "correct")

- **GameCube ≠ wbfs.** Wii → `wbfs/Title [ID6]/ID6.wbfs`; GameCube →
  `games/Title [ID6]/game.iso` (Nintendont needs plain ISO). The user has asked
  more than once assuming both are wbfs — the answer is firmly no.
- **Vimm download is a GET, despite the form saying POST.** The vault page's
  form has `method="POST"` but its `onsubmit="submitDL(...)"` handler rewrites it
  into `GET https://<dlhost>/?mediaId=<id>`. Anything else returns **400 "A
  browser error has occurred"** — no combination of headers, cookies, Sec-Fetch-*
  or field tweaks makes a POST work. This cost hours; do not re-litigate it.
- **Vimm renamed its download hosts**: `downloadN.vimm.net` are dead (no DNS),
  it's now `dlN.vimm.net`, and the endpoint is the host **root**, not
  `/download/`. Always scrape the form's own `action` (may be protocol-relative
  `//dl3.vimm.net/`) instead of assuming a host.
- **Share cookies** between the vault-page GET and the download GET
  (`_COOKIES`/`_OPENER`). Note `PHPSESSID` is host-only for `vimm.net`, so it is
  *not* sent to `dlN.vimm.net` — that's expected, not the failure cause.
- **HTTP 429 = rate limit**, and it means the request shape was *right*. Back off
  a few minutes rather than "fixing" the request.
- **`mediaId` is not the vault number** (vault `17746` → mediaId `9281`).
- **Multi-disc lives in a `media` JSON array** embedded in the vault page: one
  entry per disc with `ID` (mediaId), base64 `GoodTitle`, `SortOrder`,
  `ZippedText`. RE4 vault 7685 = disc 1 (`6461`), disc 2 (`13560`), and a
  **Preview Disc** (`31325`) whose game code differs (`D4BE` vs `G4BE`) — so it
  is NOT disc 3. Group by the ID6 read off each image: same ID6 → `game.iso` +
  `disc2.iso` in one folder; different ID6 → its own folder. Don't special-case
  disc numbers from the dropdown (it's a static 3-option menu).
- **Vimm ships GameCube as `.ciso` only** — format `alt`: `0`=.ciso,
  `1`=.nkit.iso, `2`=.rvz. There is **no plain-ISO option**, so `wit` is
  effectively required for GameCube.
- **Vimm search markup** uses `href= "/vault/NNNN"` (space after `href=`) with
  empty `/vault/999999` decoy anchors and `/images/flags/<region>.png` for region.
- **GameTDB art path is `wii/` for BOTH Wii and GameCube** (GC 404s under
  `gamecube/`). Types: `cover`→2D, `cover3D`→3D(root), `coverfull`→full,
  `disc`→disc.
- **Disc header magic**: `0x5D1C9EA3` at 0x18 = Wii; `0xC2339F3D` at 0x1C = GC.
  ID6 offsets differ per container: **`.iso`/`.gcm` → 0, `.wbfs` →
  `1 << header[8]` (usually 0x200), `.ciso` → `0x8000`** (CISO header + block map
  precede the disc). Missing the CISO case silently filed a US retail disc under
  a Japanese demo's ID — see the fuzzy-fallback note below.
- **titles.txt format**: `ID6 = Title`, one per line, from
  `https://www.gametdb.com/titles.txt?LANG=EN`.

## Architecture / where things are (top → bottom of `wiivault.py`)

- **Config**: `load_config` / `save_config`, `DEFAULTS`, path constants.
- **Constants**: `DISC_EXTS`, `ARCHIVE_EXTS`, `EMU_EXT_SYSTEM` (ext→system),
  `ROM_ALL`, `ART_BASE`, `COVER_TYPES`.
- **HTTP**: `http_get` (adds browser UA + Referer), `_get_png`.
- **GameTDB titles**: `ensure_titles` (download+cache), `title_for` (ID6→name,
  with region-swap fallback).
- **Covers**: `region_candidates` (ID6 region char → art language folders),
  `download_covers`.
- **NKit**: `is_nkit`, `denkit` (shells `nkit -task convert`).
- **Disc ID**: `read_disc_info` → `(id6, kind)` where kind ∈ {wii, gc, unknown}.
- **Vimm**: `vimm_search` (cleans the query via `clean_query` — strips special
  chars so "close" names match), `find_media` (mediaId+host+system scrape),
  `vimm_download`.
- **Queue input**: `expand_targets` + `read_list_file` — `get`/`install` accept
  quoted names, ids, urls, and list files (path passed as an arg, or via `-f`;
  `#` comments and blanks ignored). `get` has alias `install`, `targets` is
  `nargs="*"` so a bare `-f list.txt` works.
- **Unpack/route**: `unpack` (archive→ROM list), `gather` (files+dirs→ROM list),
  `install` (disc→USB Loader GX + covers), `install_emu` (cartridge→emu dir),
  `place_rom` (classifier that picks install vs install_emu), `wit_copy`.
- **Pipeline**: `process_target` = `download_target` (resolve/dedup/claim/download
  all discs → job dict) + `install_job` (extract/convert/write, releases the vault
  claim). `run_pipeline(items, cfg, args, on_status)` drives both `get` and
  `queue run`: sequential by default, or `--parallel` (one downloader thread +
  one installer thread, bounded handoff Queue(maxsize=1), so a game converts while
  the next downloads — Vimm allows 1 download at a time anyway). In parallel the
  global `QUIET` suppresses the download progress bar + wit's stdout so the two
  threads don't garble output.
- **Two separate space checks — don't merge them.** `--min-free GIB` is a *pre-run
  floor* (`run_pipeline`): if the drive is already below it, the run stops. The
  per-disc guard in `install_job` is a *fit* check: defer only when
  `free < disc + WRITE_MARGIN_GIB` (0.25). These were once additive, which meant a
  3 GiB game needed 4 GiB free and got deferred at 3.5 — a game that fits must go
  through. `deferred` keeps the entry pending and the archive cached, so it retries.
- **Backup drive** (optional): `backup_dir` + `backup_enabled` config,
  `--backup-dir` / `--no-backup` per run, `config --backup` / `--no-backup` to
  toggle without clearing the path. `backup_cfg()` returns a cfg view with the roots
  repointed, so `dest_for`/`is_installed`/`free_gib` work on it unchanged (returns
  None when off); `mirror_to_backup()` copies the *built* file (plus `.wbf1…` split
  parts) rather than re-converting — the conversion already happened, never back up
  from the download cache. It runs even when the disc was already on the primary, so
  adding a backup drive later backfills it. A backup that is absent, full or
  unwritable only warns — it never fails the primary.
- **`backup` command** (`cmd_backup` → `sync_backup`): bulk catch-up for a library
  installed before the backup drive existed. Compares by path relative to the drive
  root across `wbfs/` + `games/` (the layout is identical on both sides, so the
  relative path *is* the identity — no ID6 matching needed), copies what's missing,
  re-copies size mismatches. `copy_atomic()` writes `<name>.part` then renames, so a
  dropped share or Ctrl-C leaves no half-file that a later run reads as complete;
  `_disc_files()` ignores `.part` debris for the same reason.
- **`transfer` command** (`cmd_transfer`): the *reverse* of `backup` — pulls games
  from the backup drive onto the main one, via a curses picker. Logic is kept out of
  the curses layer so it's testable: `build_transfer_rows()` merges both drives into
  rows (one per **game folder**, so split parts and `disc2.iso` stay together),
  `transfer_plan()` derives (copy, remove) from the selection, `projected_free()`
  does the live space arithmetic, `apply_transfer()` executes. Only `transfer_tui()`
  touches curses. Optional `SYSTEM` arg filters to Wii or GameCube.
  - A row starts selected iff the game is already installed. Selecting queues a
    copy; **deselecting an installed game deletes it** from the destination. That
    is the destructive path — `apply_transfer()` refuses any target that isn't a
    directory directly under the destination's own `wbfs/` or `games/`, and
    `cmd_transfer` demands a second confirmation for removals with no backup copy.
  - When drawing, group headers consume a line too, so *every* row write re-checks
    the `h - 4` footer boundary. Getting this wrong silently paints game rows over
    the free-space readout.
  - Non-TTY (piped) exits with the list printed rather than blowing up inside curses.
  - `f` cycles the views from `row_filters()` (all / new / added < N days). The view
    is a filtered *projection* — selections live on the row dicts, so they survive a
    filter change and `transfer_plan()` always sees every row. `a`/`n` deliberately
    act on visible rows only, so a filter scopes bulk actions.
  - **Never bind bare ESC (27) to cancel.** Arrow keys *are* escape sequences and a
    split read hands back a lone 27, which silently threw away the selection
    mid-navigation. `getkey()` decodes `ESC [ A/B/5/6` by hand because keypad
    translation can't be relied on over a PTY/remote terminal, and returns -1 for
    anything unrecognised.
- **`--to-backup`** (`apply_out`): installs *onto* the backup drive and leaves the
  main one alone — for queueing games while the main drive is small or unplugged,
  then pulling them across with `transfer`. It reuses `backup_cfg()` to repoint the
  roots, then sets `backup_enabled = False` so nothing mirrors back onto itself. The
  space guard and dedup then measure the backup drive, which is what you want.
- **`sweep_partials()`** runs at the start of `backup` and `transfer`: a killed copy
  leaves `<name>.part` on purpose (better than a truncated `game.iso`), but it wastes
  space until retried. Also removes a game folder left empty by one.
- **mtime is best-effort.** `copy_atomic` copystats so "recently added" tracks when a
  game was *acquired*, not when a bulk backup ran — but a Windows-mounted drive under
  WSL (DrvFs, e.g. `/mnt/s`) refuses `utime` with EPERM, while cifs mounts accept it.
  Verified on both. The picker reads mtime from the source side, so the filter stays
  correct even when the destination can't hold a timestamp.
- **UNC paths**: `resolve_share()` normalises `\\host\share` → `//host/share` and
  returns a mount hint. Linux/WSL cannot open a UNC path directly — it must be
  cifs-mounted — so surface the hint rather than a bare "not found".
- **Demo filter**: `is_demo_disc()` / `DEMO_RE` drops demo/kiosk/preview/taikenban
  discs a vault bundles (in `download_target`, after `find_media`, before n_discs)
  unless the request asked for one (`--demos`, `--disc`, or the name/target says
  demo). Keeps "bonus" discs (real content). `--demos` on get + queue run.
- **Region pick**: `pick_result(results, query, assume_yes, region="US")` ranks by
  fuzzy title then region preference (`REGION_ALIASES`, `_region_token`). Same
  game in many regions is NOT ambiguity — it auto-picks the preferred region (US
  default) even without `-y`; only a *different* close title triggers a prompt.
  Falls back to closest region with a note. Config key `region`, flag `-r`.
- **Commands**: `cmd_get`, `cmd_search`, `cmd_organize`, `cmd_covers`,
  `cmd_config`, `cmd_update_db`.
- **CLI**: `build_parser`, `main`.

`install._titles` is set (a bit hacky) by each command before calling `install`
/ `place_rom`, so the title map is available without threading it through args.

## Strengths

- **Robust identification** — reads ID6 + system from the disc itself, so it
  doesn't trust filenames. Handles ISO, WBFS, and (via `wit`) container formats.
- **One-pass mixed routing** — a folder with Wii + GameCube + SNES + zipped games
  sorts correctly in a single `import`.
- **Honest degradation** — when `wit`/`nkit`/`7z` are missing it warns clearly
  and never writes a broken/half-converted file (NKit is skipped, not mangled).
- **Zero dependencies** — stdlib only; easy to run anywhere.
- **Well-tested offline paths** — ID reader, routing, covers, config, search
  parsing all exercised with forged fixtures and live GameTDB/Vimm reads.

## Gotchas found in real runs

- **A saved `config.json` overrides `DEFAULTS`.** Editing `DEFAULTS` in the code
  does nothing on this machine if the key already exists in the user's config.
  Any default change needs a matching `python3 wiivault.py config --<key> <val>`
  to actually take effect. (Cover art silently kept going to the old path
  because of this.)
- **`wit` exit code 108 = SYNTAX ERROR**, not a conversion failure. If you see
  108, an option is malformed — check it before suspecting the image.
  - `--split-size 4gb` is **invalid**; wit wants `4G` / `4095M` / a byte count.
  - **FAT32's max file is 4 GiB − 1**, so `4G` (exactly 4 GiB) is one byte too
    large. Default is now **`4095M`**.
- **`--trunc` is required for GameCube ISO output.** wit preallocates the disc
  geometry *declared* by the source, not the real content. RE4's Preview Disc
  declares 4.38 GiB while holding only 560 MiB, which blew past FAT32's file cap
  and failed with a misleading **"No space left on device"** (there were 51 GiB
  free). `--trunc` writes the true size (1,459,978,240 = standard GC disc).
- **Only WBFS gets split.** GameCube must stay a single `game.iso` for
  Nintendont, so it has no fallback for an oversized image — hence the
  `FAT32_MAX` post-write guard in `install()`.
- **Drive is FAT32** (Nintendont does **not** support NTFS — an NTFS drive runs
  Wii games fine and silently fails every GameCube title). exFAT also works.
- **FAT/exFAT/drvfs targets reject `utime`.** `shutil.copy2` crashes with
  `PermissionError: [Errno 1] Operation not permitted` on the copystat step when
  writing to a mounted Windows USB (`/mnt/h`). Fixed by using `shutil.copyfile`
  (data only, no metadata) for all raw-copy paths. Don't reintroduce `copy2`.
- **H: is a Windows USB, not always mounted.** Mount with
  `sudo mount -t drvfs H: /mnt/h` (needs the user's sudo; agent can't). Verify
  `/mnt/h` exists before an install or it silently writes into the WSL fs.
- **Windows interop is unavailable** — `cmd.exe`/`powershell.exe` are not on
  PATH, so the Windows side can't be driven from here (couldn't query the
  filesystem or delete a stale file). A killed `wit` can leave an unlinkable 9p
  handle (`-????????? ?`); it clears once all holding processes exit.
- **Archives can already contain `.wbfs`** (not just ISO). Wii Sports (USA) 7z
  held a trimmed `RSPE01.wbfs` — so no `wit` was needed.
- **Don't run heavy jobs while the user has their own import going** — check
  `pgrep -f "wiivault[.]py"` first. Also beware `pkill -f "wiivault.py install"`:
  the pattern matches the agent's own shell command and kills it (exit 144). Use
  a self-safe pattern like `wiivault[.]py`.

## Weaknesses / risks

- **Vimm scraping is brittle.** `find_media`, `vimm_search`, the `media` JSON
  parse, and the download URL all depend on Vimm's current HTML. It changed
  under us mid-session (hosts + method). First thing to check if `get` breaks.
- **The filename fuzzy fallback is dangerous** and was the worst bug of the
  session: with the CISO offset unknown, `guess_id_from_name` matched a US retail
  Super Monkey Ball 2 to a **Japanese demo** (`DM2J8P`), producing a wrong folder
  AND wrong cover art. Threshold is now 0.92 with a loud warning. Prefer fixing
  ID reading over loosening this.
- **Nothing has been booted on a real Wii yet.** Everything is verified at the
  file level (`wit LIST` reports valid images with matching IDs/titles), but no
  game has actually launched. RE4's `disc2.iso` swap is convention applied from
  docs, unverified in practice.
- **NKit path still unexercised** — `nkit` is not installed; no `.nkit.iso` has
  been converted.
- **`.iso` is assumed Nintendo.** PS1/Saturn/etc. disc images will be misrouted
  unless `-s` is given. No CD-based retro detection.
- **Retro is filename-only.** No DAT/No-Intro renaming; `emu_dir` layout is a
  best-guess convention that may not match the user's specific loader.
- **NKit CLI syntax is assumed** (`-task convert`); some images need `-task
  recover`. Not all NKit edge cases handled.
- **`install._titles` global-ish coupling** — if you add a code path that calls
  `install`/`place_rom` without first setting `install._titles`, it will
  `AttributeError`. Set it (via `ensure_titles`) at the top of any new command.

## How to work on it (dev tips)

- **Test without a real drive or ROMs**: forge disc images with the right magic
  bytes and point config at a temp dir. Pattern used before:
  ```python
  def wii(id6): b=bytearray(0x40); b[0:6]=id6; b[0x18:0x1c]=b"\x5d\x1c\x9e\xa3"; return bytes(b)
  def gc(id6):  b=bytearray(0x40); b[0:6]=id6; b[0x1c:0x20]=b"\xc2\x33\x9f\x3d"; return bytes(b)
  # WBFS: header b"WBFS", byte[8]=9, disc header copy (with magic) at 0x200
  ```
  Then `config --wii-dir /tmp/drive …` and run `import`/`organize` with `-n`.
- **Live checks are fine** — GameTDB (`titles.txt`, `art.gametdb.com`) and Vimm
  search are reachable and cheap; the WSL box has network + `python3`.
- **Web research tools** (`WebFetch`/`WebSearch`) are available and were used to
  pin down Vimm/GameTDB/USB-Loader-GX facts; use them before guessing at scheme
  or URL details.
- **Reset state** between tests: `rm ~/.config/wiivault/config.json` restores
  `/mnt/h` defaults; clear `~/.cache/wiivault/extract` for stale extractions.

## Scratch output — logs, queues, artifacts (agents: read this)

`wiivault.py` writes **no** log or queue files of its own. Every `.log` and
`batch_queue.txt` that has ever appeared in this repo was created by an agent
redirecting run output (`… > _stage2.log`) or hand-rolling a work list. Nine
log files and one queue file were committed that way and had to be purged from
history — don't repeat it.

If you create a file by hand during a run, put it in the right folder:

| Kind | Goes in | Example |
|---|---|---|
| Run/console output, dry-run transcripts, watch loops | `logs/` | `logs/_stage2.log` |
| Batch lists, work queues, pending-item files | `queue/` | `queue/batch_queue.txt` |

Both are gitignored, so scratch stays local and out of commits.

- **Redirect straight into the folder** — `python3 wiivault.py … > logs/run.log`.
  Don't write to the repo root and plan to move it later; you'll forget.
- **Never `git add -A` / `git add .`** without checking `git status` first — that
  is exactly how the purged logs got committed. Stage named paths.
- **Don't commit scratch to "show your work."** Quote the relevant lines in your
  response instead; the file stays local.
- If a file is genuinely a deliverable (a design doc, a fixture), it belongs in
  the repo proper with a real name — not in `logs/` or `queue/`.

## Current library state (verified with `wit LIST`)

```
wbfs/  Wii Sports [RSPE01]            Pokemon Battle Revolution [RPBE01]
games/ Super Monkey Ball 2 [GM2E8P]   Harvest Moon A Wonderful Life [GYWEE9]
       Resident Evil 4 [G4BE08]  -> game.iso + disc2.iso
       Resident Evil 4 (Preview Disc) [D4BE08] -> game.iso
covers/ <ID6>.png (3D, root) + 2D/ full/ disc/   — 24 PNGs, all 6 games
```

Covers live at `/mnt/h/covers` (user's choice over the loader's default
`apps/usbloader_gx/images` tree), which needs four Custom Paths set on the Wii:
`usb1:/covers`, `/covers/2D`, `/covers/full`, `/covers/disc`.

Installed on the box: `7z`, `wit` 3.01a. Not installed: `nkit`.

## Custom / modded game support

- `--id ID6` patches the id **into the disc** via `wit --modify all --id`
  (+`--name`). Renaming the folder alone is useless — the loader reads the id
  from the disc, so an unpatched mod collides with the base game's saves.
- `--mod [NN]` derives it from the disc's own id: `SMNE01` → `SMNE99`. Keeps the
  first 4 chars deliberately — position 4 is the **region** char, so preserving
  it keeps GameTDB art resolution working. Only the 2-char maker code changes.
- `--cover-id` fetches art under one id and files it under another (a mod
  borrowing the base game's artwork); `download_covers(..., save_as=)`.
- `--id` refuses to run against multiple ROMs; `--mod` and `--id` are exclusive.

## Likely next tasks (from user direction)

- **Boot-test on the real Wii** — especially RE4 disc swapping (`disc2.iso`).
- Retro loaders CONFIRMED: **Snes9x GX** (SNES → `snes9xgx/roms/`), **Wii64/Not64**
  (N64 → `wii64/roms/`), **WiiSX** (PS1 → `wiisx/isos/`). Each reads its own
  folder at the drive root — see `EMU_PATHS` + `emu_rel()`; `emu_dir` is now the
  drive root (was `/mnt/h/roms`), overridable per-system via `config emu_paths`.
  These emulators often default to SD, not USB — user must point them at the
  drive or keep folders where they launch from. Retro still filename-as-is.
- Optional **DAT/No-Intro renaming** for cartridge ROMs (user chose filename-as-is
  for now, but may revisit).
- NKit path validation once `nkit` is installed.

## DONE: dedup + persistent queue (was DESIGN-queue-and-dedup.md)

Implemented per spec. `dest_for()` is the single output-path source of truth;
`install()` and `is_installed()` both use it. `install()` skips an
already-installed disc (keyed by the **effective** id6, so `--mod` builds are
distinct) unless `--force`; covers still backfill on a skip. `get`/`queue run`
share `process_target()` (no duplicated pipeline) and consult a per-drive
`<out>/.wiivault/library.json` ledger to skip the **download** when a vault is
proven installed. Queue lives at `CONFIG_DIR/queue.json`, subcommands
`add/remove/list/clear/run`, status per entry for resume.

Bugs found while building it, all fixed:
- **`Path.glob("* [ID6]")` treats `[ID6]` as a character class** — it never
  matches a literal `[ID6]` folder. `is_installed` iterates `iterdir()` +
  `endswith(f"[{id6}]")` instead. Don't reintroduce a bracket glob.
- **wit stamps output mtime from the source**, so mtime is useless for "was this
  rewritten?" in tests — use `st_ino` (temp-write+rename changes the inode).
- **Ledger vs multi-disc** (a vault → several id6s, not one): a per-vault single
  `id6` got overwritten by the bonus disc, and a whole-vault "installed?" check
  looked at disc 1 only, so a partial multi-disc install read as complete. Fixed:
  the ledger stores a **list** of `{id6,kind,disc_no}` plus the vault's total
  `n_discs`; `ledger_complete()` requires every disc recorded AND still present.
  Handles RE4 (G4BE08 discs 1-2 + D4BE08 preview) and `--disc N` subsets.
