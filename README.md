# dotlink

**An offline dotfile linker driven by one plain-text manifest.** Describe which files link or copy where, per machine, then plan, apply, check and undo the lot, with every change recorded and rolled back if something goes wrong.

No servers, no accounts, no network calls. Python 3 standard library only (pytest for tests).

## Features

- **One readable manifest.** `link SRC -> DST` and `copy SRC -> DST` lines, `#` comments, and `~` and `$VAR` expansion in paths.
- **Per-machine sections.** `host laptop` makes the lines below it apply only on that machine; `host` alone returns to shared lines.
- **Line-numbered lint.** Unknown directives, missing arrows, relative targets, undefined `$VARIABLES`, bad host names, source equal to target, and copying a directory are all reported as `file:line: error:`.
- **Clash detection.** A target can be declared once per host, and never both shared and host-specific.
- **Source-reuse warnings.** The same source used by several targets is flagged; `--strict` makes warnings fail the run.
- **Read-only plan.** `plan` shows `create`, `ok`, `update`, `conflict`, `source-missing` and `prune` for every entry before anything is touched.
- **All-or-nothing apply.** Any blocking problem stops the whole apply before a single file changes.
- **Rollback on failure.** If an I/O error hits mid-apply, every step already taken in that batch is reversed.
- **Safe conflict handling.** Existing files are refused by default; `--backup` moves them into a timestamped backups folder instead of overwriting.
- **Copies that know their history.** A copied file is updated only if you have not edited it since dotlink wrote it, checked by SHA-256. Hand edits are reported as conflicts, never silently overwritten.
- **Pruning.** Targets removed from the manifest are reported as `prune`, and `apply --prune` moves them into backups rather than deleting them.
- **Full undo.** `undo` reverses the most recent batch, restores backups, and refuses to delete files edited after apply unless you pass `--force`. Undo can be repeated batch by batch.
- **Batch history.** `history` lists every recorded apply, newest first.
- **CI gate.** `check` runs lint plus plan and exits `1` on any problem (`0` clean, `2` usage error). `--json` makes plan and check machine-readable.

## Quick start

The example manifest lives in `examples/dotfiles.dl`, with its sources in `examples/dotfiles/`. Its target paths use `$DEST`, so you can try it in a scratch folder:

```sh
export DEST="$PWD/demo-home"

# 1. Check the manifest
python3 dotlink.py lint examples/dotfiles.dl

# 2. See what would happen on a laptop (plan is read-only)
python3 dotlink.py plan examples/dotfiles.dl --host laptop

# 3. Apply it
python3 dotlink.py apply examples/dotfiles.dl --host laptop

# 4. Changed your mind? Undo the last batch
python3 dotlink.py undo examples/dotfiles.dl
```

The example manifest:

```text
link  dotfiles/vimrc        -> $DEST/.vimrc
copy  dotfiles/gitconfig    -> $DEST/.gitconfig
link  dotfiles/zshrc        -> $DEST/.zshrc

host laptop
link  dotfiles/zshrc.laptop -> $DEST/.zshrc.local
```

Sources resolve against the manifest's own folder. Running `apply` creates a `dotfiles.dl.state.json` file and a `dotfiles.dl.backups/` folder next to the manifest. Both are ignored by `.gitignore`.

## Commands

| Command | What it does |
| --- | --- |
| `lint MANIFEST [--strict]` | Parse and report errors and warnings |
| `plan MANIFEST [--host H] [--json]` | Show what `apply` would do |
| `apply MANIFEST [--host H] [--backup] [--prune]` | Make the changes |
| `undo MANIFEST [--force]` | Reverse the most recent apply |
| `history MANIFEST` | List recorded batches |
| `check MANIFEST [--host H] [--strict] [--json]` | CI gate: fails on errors, conflicts, missing sources or pending prunes |

`--host` defaults to this machine's hostname.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success, or a clean result |
| `1` | Problems: manifest errors, conflicts, missing sources, nothing to undo, or a rolled-back apply |
| `2` | Usage error: bad arguments, unreadable manifest or state file |

## How to run

Requires Python 3.10 or newer.

```sh
python3 dotlink.py --help
python3 dotlink.py check path/to/your.dl --host "$(hostname -s)"
```

## How to test

```sh
python3 -m pip install pytest
python3 -m pytest -q
```

The tests cover parsing and line-numbered errors, duplicate and host-clash detection, planning, idempotent apply, conflict blocking, backups with undo, copy updates versus hand edits, pruning, undo safety, rollback after a failed copy, host filtering, CI exit codes, batch history and JSON output. Every temporary file is created under `.test-tmp/` in the project, which is git-ignored.
