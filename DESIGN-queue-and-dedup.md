# DESIGN — output-dir dedup + persistent queue

Implementation spec for two features in `wiivault.py`. Written for an agent that
has **not** seen this conversation. Read `AGENTS.md` and `README.md`
first. All line numbers below are from the current `wiivault.py` and will drift —
re-grep the function names, don't trust the numbers blindly.

Two deliverables:

1. **Dedup on install** — before downloading/transferring a game, check whether
   it already exists in the output directory; if it does, skip it.
2. **Persistent, appendable queue** — a `queue` subcommand (`add` / `remove` /
   `list` / `clear` / `run`) backed by a stored queue file, so the user can build
   up a batch across multiple invocations and then run it.

Prefixes (`Wii:` / `GameCube:` / `gc:`) already work via `_parse_entry` (~line
880) — **do not rebuild them**, just reuse them for queue entries.

---

## Hard constraint that shapes both features: ID6 comes from the disc header

The tool identifies a disc by reading its **ID6 from the disc header**
(`read_disc_info`, ~line 956 in `cmd_get`), *not* from the filename or the Vimm
page. This is the single most important fact for the dedup feature:

- For **`import`/`organize`** (local files): the ID6 is readable *before* we copy
  anything → a true pre-copy skip is easy and reliable.
- For **`get`** (download): the ID6 is **not known until after the archive is
  downloaded and extracted** (line 956). The Vimm search/vault page gives a
  title + region, not an ID6. So we **cannot** reliably compute the output path
  from a name/vault id alone on the first encounter.

Design consequence: the **authoritative** dedup check is at install time, keyed
by ID6, and it covers both paths. A *pre-download* skip for `get` is possible
only as a best-effort optimization backed by a learned `vault_id → id6` index
(see the ledger below). Build the authoritative check first; the pre-download
optimization is layered on top and must degrade gracefully.

---

## Shared foundation (build this first — both features use it)

### 1. Extract the destination-path logic into one function

Right now `install()` (~line 645-653) computes the output path inline:

```python
# wii
folder = Path(cfg["wii_dir"]) / "wbfs" / f"{safe_name(title)} [{id6}]"
dest   = folder / (f"{id6}.wbfs" if disc_no <= 1 else f"{id6}_disc{disc_no}.wbfs")
# gc
folder = Path(cfg["gc_dir"]) / "games" / f"{safe_name(title)} [{id6}]"
dest   = folder / ("game.iso" if disc_no <= 1 else f"disc{disc_no}.iso")
```

Pull this into a single helper so the dedup check and `install()` share **one**
definition (otherwise they will drift and the skip will look in the wrong place):

```python
def dest_for(id6, kind, title, cfg, disc_no=1):
    """Canonical output path for a disc. Single source of truth for both
       install() and the 'already installed?' check."""
    ...
    return folder, dest
```

Then `install()` calls `dest_for(...)` instead of computing inline.

### 2. `is_installed()` — the authoritative check (filesystem is the source of truth)

```python
def is_installed(id6, kind, title, cfg, disc_no=1, min_bytes=1<<20):
    folder, dest = dest_for(id6, kind, title, cfg, disc_no)
    # Match on the [ID6] folder, not the human title, because the title can
    # change (region/GameTDB updates) while the ID6 is stable. Prefer globbing
    # for "* [ID6]" so a renamed folder still counts as installed.
    ...
    return dest.exists() and dest.stat().st_size >= min_bytes
```

Match by **`[ID6]`**, not by title string — titles can change between runs
(GameTDB updates, region swaps) but the ID6 folder tag is stable. A glob like
`<wbfs_or_games_root>/* [<ID6>]/` finding a non-trivial `.wbfs`/`.iso` inside =
installed. The filesystem is authoritative: if the user deletes a game by hand,
it must re-install, so never trust a cache over an actual `exists()` check.

### 3. Library ledger — `vault_id → id6` index (optional accelerator only)

A small JSON that lets `get` skip the **download** on games it has installed
before. It is a cache, never the source of truth.

- Location: `<out>/.wiivault/library.json` (per-drive, travels with the disk).
  Fall back to `CONFIG_DIR/library.json` if the out root isn't writable.
- Shape: `{ "<vault_id>": {"id6": "...", "kind": "wii|gc", "title": "...",
  "system": "Wii", "installed_at": "<iso8601>"} }`.
- Written at the end of a successful `install()` in `cmd_get` (the vault_id is in
  scope there, and the id6 is known at line 956).
- Read by `get` right after `resolve_target`/`pick_result` yields a `vault_id`:
  if `vault_id` is in the ledger **and** `is_installed(entry.id6, ...)` is true →
  skip download entirely.

First-time games have no ledger entry, so they download once, then install()'s
authoritative check still prevents the redundant copy and records the mapping.

---

## Feature A — dedup on ingest/download

### Behavior

- **Default: skip if already installed.** In `place_rom`/`install` for `import`,
  and in the `get` disc loop, before the expensive copy/convert, call
  `is_installed(...)`. If true, print `ok(f"already installed, skipping {title}
  [{id6}]")` and return without copying.
- **`get` pre-download optimization:** consult the ledger (above) to skip the
  Vimm download too when we can prove it's already there. If unknown, download,
  then let the install-time check catch it.
- **`--force` / `--overwrite` flag** on `get` and `organize` to bypass the skip
  and re-copy (needed to replace a corrupt file or re-patch a `--mod` build).
  Wire it into `build_parser` next to `--dry-run` (~line 1105 / 1138).
- **Covers on skip:** when skipping the disc copy, still ensure art exists —
  call `download_covers(...)` (it already no-ops per-type when the file is
  present, see ~line 249). This lets a re-run backfill missing covers without
  re-copying 2 GB. Consider a `--refresh-covers` flag if you want to force it.
- **`--mod` / `--id` builds:** the effective (custom) ID6 is what defines the
  folder. `install()` already resolves `id6 = custom_id or ...` before computing
  the path — make sure `is_installed` is called with the **effective** id6, not
  the disc's real id6, or mods will wrongly look already-installed.
- **Multi-disc:** the check is per `(id6, disc_no)`. GameCube disc 2 lives beside
  game.iso; a bonus disc with its own game code is a separate folder. `dest_for`
  already encodes this — pass `disc_no` through.

### Fix the misleading message

The 429 warning at ~line 480-482 already claims *"already-installed games are
skipped."* — which is currently **false**. Once this feature lands the claim
becomes true; leave it, but until then it overstates behavior. Don't add new
copies of that claim elsewhere until the skip actually exists.

### Touch points

- `install()` ~line 627 — add the `is_installed` guard after id6/title/kind are
  resolved (after ~line 643) and before `folder.mkdir` (~line 677). Respect
  `dry_run` (report "would skip") and `force`.
- `cmd_get` disc loop ~line 944-967 — add the ledger pre-download check before
  `vimm_download` (~line 945); write the ledger after a successful `install`.
- `place_rom` ~line 757 / `cmd_organize` — thread a `force` kwarg through.

---

## Feature B — persistent, appendable queue

### Storage: one queue file, not many

The user floated chaining multiple files ("go from one file to the next"). Prefer
a **single canonical queue store** instead — it makes dedup, ordering, removal,
and resume trivial, whereas a list-of-files makes all four awkward. `queue add
<file>` should **read that file and append its lines into the store**, not track
the file by reference. (If the user later wants to keep source files linked,
that's a separate feature; don't build it now.)

- Location: `CONFIG_DIR/queue.json` (`~/.config/wiivault/queue.json`). Global, not
  per-drive — the queue is "what to fetch," independent of the target disk.
- Format (JSON array, ordered), one object per entry so we can track status for
  **resume** (an explicit AGENTS.md "likely next task"):

  ```json
  [
    {"line": "Zack & Wiki", "system": "Wii", "status": "pending",
     "added_at": "2026-07-18T00:00:00Z", "note": ""}
  ]
  ```

  `status ∈ {pending, installed, failed, skipped}`. `line` + `system` are exactly
  what `_parse_entry` produces, so parsing/adding reuses existing code.
- Keep it human-friendly: `queue add -f any_list.txt` accepts the existing plain
  list format (`read_list_file` + `_parse_entry`, ~lines 870-887), and provide
  `queue list --format text` to dump it back out in that same format.

### Subcommand shape (recommended: actions, not bare flags)

The user weighed `queue add` vs `queue -a`. Recommend **action subparsers** — it
reads like `git remote add`, self-documents in `--help`, and lets each action
carry its own flags (e.g. `add -f`). Offer `-a/-d` as aliases only if desired.

```
wiivault queue add <entries...>     # names / vault ids / vimm URLs / "Wii: Name"
wiivault queue add -f LIST          # append every line of LIST (repeatable)
wiivault queue add -                # append from stdin
wiivault queue remove <entries...>  # remove by fuzzy name/id match (normalize())
wiivault queue remove -i N          # remove by 1-based index shown in `list`
wiivault queue list                 # show index, entry, system, status
wiivault queue clear                # empty the queue (confirm unless -y)
wiivault queue run [get flags...]   # drain pending entries through the get pipeline
```

- `add` dedups against what's already queued (normalized compare, reuse
  `normalize()` ~line 167) and — nice tie-in with Feature A — warns/skips if the
  entry is already installed (needs a resolvable id; for names this is best
  effort, so just annotate, don't hard-block).
- `run` is the important one: it must reuse the **existing** `get` logic, not a
  copy. Refactor the per-target body of `cmd_get` (~lines 916-967) into a shared
  helper:

  ```python
  def process_target(target, system, cfg, args) -> status
  ```

  Then both `cmd_get` and `queue run` iterate their targets calling it. `run`
  updates each entry's `status` as it goes (installed/failed) so an interrupted
  run resumes by re-running (pending + failed retried, installed skipped). Add
  `queue run --keep-installed` if you'd rather not prune on success; default
  should drop or mark `installed`.
- Any new command that calls `install`/`place_rom` **must set
  `install._titles`** first (via `ensure_titles(cfg)`) or it `AttributeError`s —
  this is a known footgun documented in AGENTS.md. `cmd_get` does it at ~line 911.

### CLI wiring

Register the subcommand in `build_parser` (~line 1086, alongside `get`/`organize`
at 1088/1117) with a nested subparser for the actions, and a `cmd_queue(args)`
dispatcher `set_defaults(func=cmd_queue)`. `queue run` should accept the same
output/region/covers flags `get` uses (`-o`, `-r`, `-y`, `--covers/--no-covers`,
`--force`) so a drained queue honors the same options.

---

## Testing (match the existing offline-first style)

Per AGENTS.md, test without a real drive by forging disc images with the right
magic bytes and pointing config at a temp dir. Specifically:

- **`dest_for` / `is_installed`:** create `temp/wbfs/Foo [RTZE08]/RTZE08.wbfs`,
  assert `is_installed("RTZE08","wii",...)` is true; assert a bogus id is false;
  assert a renamed folder `Bar [RTZE08]/` still counts (glob-by-id6).
- **Skip path:** forge a Wii disc, run `import` twice; second run must print
  "already installed" and **not** rewrite the file (compare mtime/inode).
- **`--force`:** same, but with `--force` the file is rewritten.
- **Queue:** `queue add A`, `queue add -f list.txt`, `queue list` ordering,
  `queue remove` by name and by index, dedup on re-add, `queue run -n` (dry run)
  drains without writing, status transitions on success/failure.
- **Prefix reuse:** `queue add "GameCube: Wind Waker"` stores `system=GameCube`.

Live GameTDB/Vimm reads are fine and cheap (see AGENTS.md) but not needed for the
dedup/queue logic — keep those unit tests offline.

---

## Acceptance criteria / checklist

- [ ] `dest_for()` is the only place output paths are computed; `install()` uses it.
- [ ] `is_installed()` matches by `[ID6]`, treats the filesystem as authoritative.
- [ ] `import` skips an already-installed disc before copying; `--force` overrides.
- [ ] `get` skips the copy via the install-time check; skips the **download** too
      when the ledger proves it's installed; degrades gracefully when it can't.
- [ ] Skipping still backfills missing covers; `--mod`/`--id` use the effective id6.
- [ ] `queue add/remove/list/clear/run` work on `CONFIG_DIR/queue.json`.
- [ ] `queue add` accepts names, ids, URLs, `System: Name` prefixes, and `-f FILE`.
- [ ] `queue run` reuses a shared `process_target` (no logic duplicated from `get`)
      and updates per-entry status so it resumes after interruption.
- [ ] New queue command sets `install._titles` before installing.
- [ ] Offline tests cover dest/skip/force/queue-ops; existing tests still pass.
- [ ] `README.md` documents `queue` and the dedup behavior; `AGENTS.md`
      "likely next tasks" updated (persistent queue / dedup now done).

## Explicitly out of scope

- Multi-file queue chaining (rejected in favor of one store; revisit only if asked).
- Retro/cartridge systems as queue prefixes (queue prefixes stay Wii/GameCube/gc;
  retro remains `import -s SYSTEM` on local files).
- Any change to how ID6 is read or how covers are laid out.
