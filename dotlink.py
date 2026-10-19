#!/usr/bin/env python3
"""dotlink: an offline dotfile linker driven by one plain-text manifest.

Commands
  lint     check the manifest; line-numbered errors and warnings
  plan     show what apply would do, touching nothing
  apply    create links and copies (all-or-nothing, rolled back on failure)
  undo     reverse the most recent apply
  history  list recorded apply batches
  check    CI gate: lint plus plan; exits 1 on any problem

Manifest lines
  host NAME         following entries apply only on machine NAME ('host' alone resets)
  link SRC -> DST   symlink DST to SRC
  copy SRC -> DST   copy SRC to DST and remember its checksum
  # comment         ignored
"""

from __future__ import annotations

import argparse
import copy
import filecmp
import hashlib
import json
import os
import re
import shutil
import socket
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2

STATE_VERSION = 1
DIRECTIVES = ("link", "copy")
PROBLEM_OPS = ("conflict", "source-missing", "prune")

_COMMENT = re.compile(r"(^|\s)#.*$")
_HOST_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class DotlinkError(Exception):
    """A failure that stops a command; `code` is the process exit status."""

    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Entry:
    kind: str
    source: str
    target: str
    host: str | None
    line: int


@dataclass(frozen=True)
class Action:
    op: str  # ok, create, update, conflict, source-missing, prune
    kind: str  # link or copy
    target: str
    source: str | None
    line: int | None
    note: str = ""


@dataclass
class Manifest:
    entries: list[Entry]
    errors: list[tuple[int, str]]
    warnings: list[tuple[int, str]]


# ---------------------------------------------------------------- manifest


def expand(text: str) -> str:
    return os.path.expanduser(os.path.expandvars(text))


def parse_manifest(text: str, base_dir: str) -> Manifest:
    """Parse manifest text. Relative sources resolve against base_dir."""
    entries: list[Entry] = []
    errors: list[tuple[int, str]] = []
    warnings: list[tuple[int, str]] = []
    declared: dict[str, list[tuple[str | None, int]]] = {}
    sources: dict[str, list[int]] = {}
    host_lines: dict[str, int] = {}
    host_counts: Counter[str] = Counter()
    host: str | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        parts = line.split(None, 1)
        word = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if word == "host":
            if rest in ("", "*"):
                host = None
            elif _HOST_NAME.fullmatch(rest):
                host = rest
                host_lines.setdefault(rest, lineno)
            else:
                errors.append((lineno, f"invalid host name {rest!r}"))
            continue

        if word not in DIRECTIVES:
            errors.append((lineno, f"unknown directive {parts[0]!r} (use host, link or copy)"))
            continue

        src_text, arrow, dst_text = rest.partition("->")
        src_text, dst_text = src_text.strip(), dst_text.strip()
        if not arrow or not src_text or not dst_text:
            errors.append((lineno, f"expected '{word} SOURCE -> TARGET'"))
            continue

        expanded_src, expanded_dst = expand(src_text), expand(dst_text)
        if "$" in expanded_src or "$" in expanded_dst:
            errors.append((lineno, "undefined environment variable in path"))
            continue

        source = os.path.normpath(os.path.join(base_dir, expanded_src))
        target = os.path.normpath(expanded_dst)
        if not os.path.isabs(target):
            errors.append((lineno, f"target must be absolute or start with ~: {dst_text!r}"))
            continue
        if source == target:
            errors.append((lineno, "source and target are the same path"))
            continue
        if word == "copy" and os.path.isdir(source):
            errors.append((lineno, "copy source is a directory; use link"))
            continue

        # A target may appear once per host, and never both shared and host-specific.
        clash = [ln for h, ln in declared.get(target, []) if h is None or host is None or h == host]
        if clash:
            errors.append((lineno, f"target {target} already declared on line {clash[0]}"))
            continue
        declared.setdefault(target, []).append((host, lineno))
        sources.setdefault(source, []).append(lineno)
        if host is not None:
            host_counts[host] += 1
        entries.append(Entry(word, source, target, host, lineno))

    for source, lines in sources.items():
        if len(lines) > 1:
            where = ", ".join(str(n) for n in lines[1:])
            warnings.append((lines[0], f"source {source} is also used on line {where}"))
    for name, lineno in host_lines.items():
        if not host_counts[name]:
            warnings.append((lineno, f"host section {name!r} has no entries"))
    return Manifest(entries, errors, warnings)


# ------------------------------------------------------------------- state


def state_paths(manifest: Path) -> tuple[Path, Path]:
    """The state file and backup folder that live beside the manifest."""
    return (
        manifest.with_name(manifest.name + ".state.json"),
        manifest.with_name(manifest.name + ".backups"),
    )


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "installed": {}, "history": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DotlinkError(f"cannot read state file {path}: {exc}")
    if state.get("version") != STATE_VERSION:
        raise DotlinkError(f"state file {path} has an unsupported version")
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def link_points_to(link: str, source: str) -> bool:
    dest = os.readlink(link)
    return os.path.normpath(os.path.join(os.path.dirname(link), dest)) == source


# -------------------------------------------------------------------- plan


def plan_entry(entry: Entry, installed: dict) -> Action:
    record = installed.get(entry.target)

    def act(op: str, note: str = "") -> Action:
        return Action(op, entry.kind, entry.target, entry.source, entry.line, note)

    if not os.path.exists(entry.source):
        return act("source-missing", f"source {entry.source} does not exist")
    if not os.path.lexists(entry.target):
        return act("create")
    if entry.kind == "link":
        if os.path.islink(entry.target):
            if link_points_to(entry.target, entry.source):
                return act("ok")
            if record and record["kind"] == "link":
                return act("update", "managed link points at a different source")
        return act("conflict", "exists and is not a link to the source")
    if not os.path.islink(entry.target) and os.path.isfile(entry.target):
        if filecmp.cmp(entry.target, entry.source, shallow=False):
            return act("ok")
        if record and record["kind"] == "copy" and record.get("sha256") == sha256_of(entry.target):
            return act("update", "source changed and the copy was not edited")
    return act("conflict", "exists and differs from the source")


def make_plan(entries: list[Entry], installed: dict, host: str) -> list[Action]:
    actions = [plan_entry(e, installed) for e in entries if e.host in (None, host)]
    declared = {e.target for e in entries}
    for target in sorted(installed):
        if target not in declared:
            record = installed[target]
            actions.append(Action("prune", record["kind"], target, record.get("source"), None,
                                  "no longer in manifest"))
    return actions


# ------------------------------------------------------------------- apply


def _move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def _record(kind: str, source: str, target: str) -> dict:
    record = {"kind": kind, "source": source}
    if kind == "copy":
        record["sha256"] = sha256_of(target)
    return record


def _reverse(ops: list[list[str]]) -> None:
    """Undo a list of recorded operations, newest first."""
    failures = []
    for op, target, other in reversed(ops):
        try:
            if op in ("link", "copy"):
                if os.path.lexists(target):
                    os.unlink(target)
            elif op == "move":
                _move(other, target)
        except OSError as exc:
            failures.append(f"{target}: {exc}")
    if failures:
        raise DotlinkError("undo incomplete: " + "; ".join(failures), EXIT_PROBLEMS)


def apply_manifest(manifest_path: Path, manifest: Manifest, host: str, *,
                   backup: bool, prune: bool) -> tuple[list[Action], str | None]:
    if manifest.errors:
        raise DotlinkError(f"manifest has {len(manifest.errors)} error(s); nothing applied (run lint)",
                           EXIT_PROBLEMS)
    state_path, backup_root = state_paths(manifest_path)
    state = load_state(state_path)
    installed = state["installed"]
    actions = make_plan(manifest.entries, installed, host)

    blocking = [a for a in actions if a.op == "source-missing" or (a.op == "conflict" and not backup)]
    if blocking:
        lines = [f"line {a.line}: {a.target}: {a.note}" for a in blocking]
        raise DotlinkError("nothing applied; resolve these first (or pass --backup):\n  "
                           + "\n  ".join(lines), EXIT_PROBLEMS)

    if all(a.op == "ok" for a in actions) and not any(a.op == "prune" for a in actions):
        for a in actions:
            installed[a.target] = _record(a.kind, a.source, a.target)
        save_state(state_path, state)
        return actions, None

    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    batch_dir = backup_root / batch_id
    before = copy.deepcopy(installed)
    ops: list[list[str]] = []
    try:
        for a in actions:
            if a.op == "prune" and not prune:
                continue
            if a.op in ("conflict", "update", "prune") and os.path.lexists(a.target):
                batch_dir.mkdir(parents=True, exist_ok=True)
                aside = str(batch_dir / f"{len(ops):03d}-{Path(a.target).name}")
                _move(a.target, aside)
                ops.append(["move", a.target, aside])
            if a.op == "prune":
                installed.pop(a.target, None)
                continue
            if a.op == "ok":
                installed[a.target] = _record(a.kind, a.source, a.target)
                continue
            # Log before creating, so a half-written copy is still cleaned up on rollback.
            ops.append([a.kind, a.target, a.source])
            os.makedirs(os.path.dirname(a.target), exist_ok=True)
            if a.kind == "link":
                os.symlink(a.source, a.target)
            else:
                shutil.copy2(a.source, a.target)
            installed[a.target] = _record(a.kind, a.source, a.target)
    except OSError as exc:
        _reverse(ops)
        raise DotlinkError(f"apply failed and was rolled back: {exc}", EXIT_PROBLEMS)

    if ops:
        state["history"].append({
            "id": batch_id,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": host,
            "ops": ops,
            "installed_before": before,
        })
    save_state(state_path, state)
    return actions, batch_id if ops else None


def undo_last(manifest_path: Path, *, force: bool) -> dict:
    state_path, _ = state_paths(manifest_path)
    state = load_state(state_path)
    if not state["history"]:
        raise DotlinkError("nothing to undo", EXIT_PROBLEMS)
    batch = state["history"][-1]
    installed = state["installed"]
    if not force:
        edited = []
        for op, target, source in batch["ops"]:
            if op == "link" and not (os.path.islink(target) and link_points_to(target, source)):
                if os.path.lexists(target):
                    edited.append(target)
            elif op == "copy" and os.path.isfile(target) and not os.path.islink(target):
                record = installed.get(target, {})
                if record.get("sha256") != sha256_of(target):
                    edited.append(target)
        if edited:
            raise DotlinkError("refusing to undo; these were changed after apply (use --force):\n  "
                               + "\n  ".join(edited), EXIT_PROBLEMS)
    _reverse(batch["ops"])
    state["installed"] = batch["installed_before"]
    state["history"].pop()
    save_state(state_path, state)
    return batch


# --------------------------------------------------------------------- CLI


def default_host() -> str:
    return socket.gethostname().split(".")[0]


def load_manifest(path: Path) -> Manifest:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DotlinkError(f"cannot read manifest {path}: {exc}")
    return parse_manifest(text, os.path.dirname(os.path.abspath(path)))


def print_messages(path: Path, manifest: Manifest) -> None:
    for lineno, msg in manifest.errors:
        print(f"{path}:{lineno}: error: {msg}")
    for lineno, msg in manifest.warnings:
        print(f"{path}:{lineno}: warning: {msg}")


def action_dict(a: Action) -> dict:
    return {"op": a.op, "kind": a.kind, "target": a.target, "source": a.source,
            "line": a.line, "note": a.note}


def print_actions(actions: list[Action]) -> None:
    for a in actions:
        where = f" (line {a.line})" if a.line else ""
        note = f": {a.note}" if a.note else ""
        print(f"{a.op:<14} {a.target}{where}{note}")
    counts = Counter(a.op for a in actions)
    summary = ", ".join(f"{counts[op]} {op}" for op in
                        ("create", "update", "ok", "conflict", "source-missing", "prune") if counts[op])
    print(summary or "no entries for this host")


def cmd_lint(args) -> int:
    manifest = load_manifest(args.manifest)
    print_messages(args.manifest, manifest)
    print(f"{len(manifest.entries)} entries, {len(manifest.errors)} errors, "
          f"{len(manifest.warnings)} warnings")
    failed = bool(manifest.errors) or (args.strict and bool(manifest.warnings))
    return EXIT_PROBLEMS if failed else EXIT_OK


def cmd_plan(args) -> int:
    manifest = load_manifest(args.manifest)
    state_path, _ = state_paths(args.manifest)
    actions = make_plan(manifest.entries, load_state(state_path)["installed"], args.host)
    if args.json:
        print(json.dumps({"host": args.host, "errors": manifest.errors,
                          "warnings": manifest.warnings,
                          "actions": [action_dict(a) for a in actions]}, indent=2))
    else:
        print_messages(args.manifest, manifest)
        print(f"plan for host {args.host}:")
        print_actions(actions)
    return EXIT_PROBLEMS if manifest.errors else EXIT_OK


def cmd_apply(args) -> int:
    manifest = load_manifest(args.manifest)
    print_messages(args.manifest, manifest)
    actions, batch_id = apply_manifest(args.manifest, manifest, args.host,
                                       backup=args.backup, prune=args.prune)
    print_actions(actions)
    if not args.prune and any(a.op == "prune" for a in actions):
        print("stale targets were left in place; pass --prune to move them to backups")
    print(f"batch {batch_id}" if batch_id else "nothing changed")
    return EXIT_OK


def cmd_undo(args) -> int:
    batch = undo_last(args.manifest, force=args.force)
    print(f"undid batch {batch['id']} ({len(batch['ops'])} operation(s))")
    return EXIT_OK


def cmd_history(args) -> int:
    state_path, _ = state_paths(args.manifest)
    history = load_state(state_path)["history"]
    if not history:
        print("no batches recorded")
    for batch in reversed(history):
        print(f"{batch['id']}  {batch['at']}  host={batch['host']}  {len(batch['ops'])} operation(s)")
    return EXIT_OK


def cmd_check(args) -> int:
    manifest = load_manifest(args.manifest)
    state_path, _ = state_paths(args.manifest)
    actions = make_plan(manifest.entries, load_state(state_path)["installed"], args.host)
    problems = [a for a in actions if a.op in PROBLEM_OPS]
    failed = bool(manifest.errors or problems or (args.strict and manifest.warnings))
    if args.json:
        print(json.dumps({"host": args.host, "pass": not failed, "errors": manifest.errors,
                          "warnings": manifest.warnings,
                          "actions": [action_dict(a) for a in actions]}, indent=2))
    else:
        print_messages(args.manifest, manifest)
        print_actions(actions)
        print("PASS" if not failed else "FAIL")
    return EXIT_PROBLEMS if failed else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dotlink", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, func, *, host: bool = False, json_flag: bool = False,
            strict: bool = False) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("manifest", type=Path, help="path to the manifest file")
        if host:
            sp.add_argument("--host", default=default_host(),
                            help="machine name that selects host sections (default: this hostname)")
        if json_flag:
            sp.add_argument("--json", action="store_true", help="print machine-readable JSON")
        if strict:
            sp.add_argument("--strict", action="store_true", help="treat warnings as failures")
        sp.set_defaults(func=func)
        return sp

    add("lint", "check the manifest for errors and warnings", cmd_lint, strict=True)
    add("plan", "show what apply would do (read-only)", cmd_plan, host=True, json_flag=True)
    apply_p = add("apply", "create links and copies", cmd_apply, host=True)
    apply_p.add_argument("--backup", action="store_true",
                         help="move conflicting files aside into the backups folder")
    apply_p.add_argument("--prune", action="store_true",
                         help="move targets no longer in the manifest into backups")
    add("undo", "reverse the most recent apply", cmd_undo).add_argument(
        "--force", action="store_true", help="undo even if files were edited since apply")
    add("history", "list recorded apply batches", cmd_history)
    add("check", "CI gate: lint plus plan; fails on any problem", cmd_check,
        host=True, json_flag=True, strict=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DotlinkError as exc:
        print(f"dotlink: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
