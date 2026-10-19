"""Tests for dotlink. Every temporary file lives under .test-tmp/ in the project."""

import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dotlink  # noqa: E402

TMP_ROOT = ROOT / ".test-tmp"


@pytest.fixture
def ws(monkeypatch):
    """A scratch workspace with a source folder, a fake home and $DEST set to home."""
    base = TMP_ROOT / uuid.uuid4().hex
    src = base / "src"
    home = base / "home"
    src.mkdir(parents=True)
    home.mkdir()
    (src / "vimrc").write_text("set number\n", encoding="utf-8")
    (src / "gitconfig").write_text("[core]\n\teditor = vim\n", encoding="utf-8")
    (src / "zshrc.laptop").write_text("# laptop\n", encoding="utf-8")
    monkeypatch.setenv("DEST", str(home))
    yield {"base": base, "src": src, "home": home, "manifest": src / "manifest.dl"}
    shutil.rmtree(base, ignore_errors=True)


def write_manifest(ws, text):
    ws["manifest"].write_text(text, encoding="utf-8")
    return ws["manifest"]


def run(*args):
    return dotlink.main(list(args))


BASIC = """\
link vimrc -> $DEST/.vimrc
copy gitconfig -> $DEST/.gitconfig
"""


def test_parse_resolves_sources_targets_and_hosts(ws):
    text = "link vimrc -> ~/.vimrc\n# comment only\n\nhost laptop\ncopy gitconfig -> $DEST/.gitconfig\nhost\nlink zshrc.laptop -> $DEST/.z\n"
    m = dotlink.parse_manifest(text, str(ws["src"]))
    assert m.errors == []
    assert [(e.kind, e.host, e.line) for e in m.entries] == [
        ("link", None, 1), ("copy", "laptop", 5), ("link", None, 7)]
    assert m.entries[0].source == str(ws["src"] / "vimrc")
    assert m.entries[0].target == str(Path.home() / ".vimrc")
    assert m.entries[1].target == str(ws["home"] / ".gitconfig")


def test_lint_reports_line_numbered_errors(ws):
    text = (
        "link vimrc -> $DEST/.vimrc\n"
        "symlink vimrc -> $DEST/.x\n"
        "link vimrc $DEST/.y\n"
        "link vimrc -> relative/.z\n"
        "link vimrc -> $NOPE_NOT_SET/.w\n"
        "host bad name!\n"
        "link vimrc -> vimrc\n"
    )
    m = dotlink.parse_manifest(text, str(ws["src"]))
    assert [line for line, _ in m.errors] == [2, 3, 4, 5, 6, 7]
    assert len(m.entries) == 1


def test_duplicate_targets_and_shared_host_clashes_are_errors(ws):
    text = (
        "link vimrc -> $DEST/.vimrc\n"
        "host laptop\n"
        "link gitconfig -> $DEST/.vimrc\n"   # clashes with shared line 1
        "host desk\n"
        "link gitconfig -> $DEST/.gitconfig\n"
        "link vimrc -> $DEST/.gitconfig\n"   # same target as line 5, same host
    )
    m = dotlink.parse_manifest(text, str(ws["src"]))
    assert [line for line, _ in m.errors] == [3, 6]
    assert "already declared on line 1" in m.errors[0][1]


def test_plan_on_fresh_tree_only_creates(ws):
    manifest = write_manifest(ws, BASIC)
    manifest_obj = dotlink.load_manifest(manifest)
    actions = dotlink.make_plan(manifest_obj.entries, {}, "this")
    assert [(a.op, a.kind) for a in actions] == [("create", "link"), ("create", "copy")]
    assert not (ws["home"] / ".vimrc").exists()


def test_apply_creates_links_and_copies_and_is_idempotent(ws, capsys):
    manifest = write_manifest(ws, BASIC)
    assert run("apply", str(manifest), "--host", "this") == 0
    vimrc = ws["home"] / ".vimrc"
    gitconfig = ws["home"] / ".gitconfig"
    assert vimrc.is_symlink() and Path(vimrc).resolve() == (ws["src"] / "vimrc").resolve()
    assert not gitconfig.is_symlink()
    assert gitconfig.read_text() == (ws["src"] / "gitconfig").read_text()
    capsys.readouterr()

    assert run("apply", str(manifest), "--host", "this") == 0
    out = capsys.readouterr().out
    assert "nothing changed" in out
    assert run("plan", str(manifest), "--host", "this") == 0


def test_conflict_blocks_whole_apply_and_changes_nothing(ws, capsys):
    (ws["home"] / ".vimrc").write_text("mine\n", encoding="utf-8")
    manifest = write_manifest(ws, BASIC)
    assert run("apply", str(manifest), "--host", "this") == 1
    assert (ws["home"] / ".vimrc").read_text() == "mine\n"
    assert not (ws["home"] / ".gitconfig").exists()
    assert "pass --backup" in capsys.readouterr().err


def test_backup_moves_conflict_aside_and_undo_restores_it(ws):
    (ws["home"] / ".vimrc").write_text("old vim\n", encoding="utf-8")
    manifest = write_manifest(ws, BASIC)
    assert run("apply", str(manifest), "--host", "this", "--backup") == 0
    assert (ws["home"] / ".vimrc").is_symlink()
    backups = list((ws["src"] / "manifest.dl.backups").rglob("*-.vimrc"))
    assert len(backups) == 1 and backups[0].read_text() == "old vim\n"

    assert run("undo", str(manifest)) == 0
    assert not (ws["home"] / ".vimrc").is_symlink()
    assert (ws["home"] / ".vimrc").read_text() == "old vim\n"
    assert not (ws["home"] / ".gitconfig").exists()


def test_copy_update_only_when_unedited(ws):
    manifest = write_manifest(ws, "copy gitconfig -> $DEST/.gitconfig\n")
    assert run("apply", str(manifest), "--host", "this") == 0

    (ws["src"] / "gitconfig").write_text("[core]\n\teditor = nano\n", encoding="utf-8")
    actions = dotlink.make_plan(dotlink.load_manifest(manifest).entries,
                                dotlink.load_state(dotlink.state_paths(manifest)[0])["installed"],
                                "this")
    assert [a.op for a in actions] == ["update"]
    assert run("apply", str(manifest), "--host", "this") == 0
    assert (ws["home"] / ".gitconfig").read_text() == "[core]\n\teditor = nano\n"

    (ws["home"] / ".gitconfig").write_text("hand edited\n", encoding="utf-8")
    (ws["src"] / "gitconfig").write_text("[core]\n\teditor = emacs\n", encoding="utf-8")
    actions = dotlink.make_plan(dotlink.load_manifest(manifest).entries,
                                dotlink.load_state(dotlink.state_paths(manifest)[0])["installed"],
                                "this")
    assert [a.op for a in actions] == ["conflict"]


def test_prune_moves_orphans_aside_and_undo_restores(ws, capsys):
    manifest = write_manifest(ws, BASIC)
    assert run("apply", str(manifest), "--host", "this") == 0

    write_manifest(ws, "link vimrc -> $DEST/.vimrc\n")
    assert run("apply", str(manifest), "--host", "this") == 0
    assert (ws["home"] / ".gitconfig").exists()
    assert "--prune" in capsys.readouterr().out

    assert run("apply", str(manifest), "--host", "this", "--prune") == 0
    assert not (ws["home"] / ".gitconfig").exists()

    assert run("undo", str(manifest)) == 0
    assert (ws["home"] / ".gitconfig").read_text() == (ws["src"] / "gitconfig").read_text()


def test_undo_refuses_edited_copy_unless_forced(ws):
    manifest = write_manifest(ws, "copy gitconfig -> $DEST/.gitconfig\n")
    assert run("apply", str(manifest), "--host", "this") == 0
    (ws["home"] / ".gitconfig").write_text("edited after apply\n", encoding="utf-8")

    assert run("undo", str(manifest)) == 1
    assert (ws["home"] / ".gitconfig").read_text() == "edited after apply\n"

    assert run("undo", str(manifest), "--force") == 0
    assert not (ws["home"] / ".gitconfig").exists()


def test_failed_apply_rolls_back_earlier_steps(ws, monkeypatch):
    manifest = write_manifest(ws, BASIC)

    def boom(src, dst, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(dotlink.shutil, "copy2", boom)
    assert run("apply", str(manifest), "--host", "this") == 1
    assert not (ws["home"] / ".vimrc").is_symlink()
    assert not (ws["home"] / ".gitconfig").exists()
    state = dotlink.load_state(dotlink.state_paths(manifest)[0])
    assert state["history"] == [] and state["installed"] == {}


def test_host_sections_only_apply_on_their_machine(ws):
    manifest = write_manifest(ws, "host laptop\nlink zshrc.laptop -> $DEST/.zshrc.local\nhost\n"
                                  "link vimrc -> $DEST/.vimrc\n")
    assert run("apply", str(manifest), "--host", "desk") == 0
    assert (ws["home"] / ".vimrc").is_symlink()
    assert not (ws["home"] / ".zshrc.local").exists()

    assert run("apply", str(manifest), "--host", "laptop") == 0
    assert (ws["home"] / ".zshrc.local").is_symlink()


def test_check_is_a_ci_gate(ws):
    manifest = write_manifest(ws, BASIC)
    assert run("check", str(manifest), "--host", "this") == 0  # fresh tree: only creates

    (ws["home"] / ".vimrc").write_text("conflict\n", encoding="utf-8")
    assert run("check", str(manifest), "--host", "this") == 1

    write_manifest(ws, BASIC + "link vimrc -> $DEST/.vimrc2\nlink gitconfig -> $DEST/.g2\n")
    # Duplicate-source warning: passes normally, fails under --strict.
    assert run("lint", str(manifest)) == 0
    assert run("lint", str(manifest), "--strict") == 1
    assert run("lint", str(ws["src"] / "missing.dl")) == 2


def test_history_lists_batches_and_undo_walks_back(ws, capsys):
    manifest = write_manifest(ws, "link vimrc -> $DEST/.vimrc\n")
    assert run("apply", str(manifest), "--host", "this") == 0
    write_manifest(ws, "link vimrc -> $DEST/.vimrc\ncopy gitconfig -> $DEST/.gitconfig\n")
    assert run("apply", str(manifest), "--host", "this") == 0
    capsys.readouterr()
    assert run("history", str(manifest)) == 0
    assert capsys.readouterr().out.count("operation(s)") == 2

    assert run("undo", str(manifest)) == 0
    assert not (ws["home"] / ".gitconfig").exists()
    assert (ws["home"] / ".vimrc").is_symlink()
    assert run("undo", str(manifest)) == 0
    assert not (ws["home"] / ".vimrc").exists()
    assert run("undo", str(manifest)) == 1


def test_plan_json_output_is_machine_readable(ws, capsys):
    manifest = write_manifest(ws, BASIC)
    assert run("plan", str(manifest), "--host", "this", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["host"] == "this"
    assert [a["op"] for a in data["actions"]] == ["create", "create"]
    assert data["actions"][0]["line"] == 1
