import io
import sys

import pytest

import octantcompass.cli as cli


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.skipif(sys.platform == "win32", reason="Windows always opens the window")
def test_no_command_on_a_terminal_shows_the_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Terminal())
    assert cli.main([]) == 2
    help = capsys.readouterr().out
    for command in ("gui", "serve", "pair", "env", "db", "shortcut"):
        assert f"    {command} " in help


def test_no_command_without_a_terminal_opens_the_window(monkeypatch):
    # A double-click: no terminal on stdin.
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    seen = []

    def parse_args(self, args):
        seen.append(args)
        raise SystemExit(0)
    monkeypatch.setattr(cli.argparse.ArgumentParser, "parse_args", parse_args)
    with pytest.raises(SystemExit):
        cli.main([])
    assert seen == [["gui"]]


def test_commands_are_a_noun_and_a_verb(tmp_path):
    parser = cli.build_parser()
    parse = lambda *args: parser.parse_args([*args, "--data-dir", str(tmp_path)])
    assert parse("pair", "code").command == "pair code"
    assert parse("env", "list").command == "env list"
    revoke = parse("env", "revoke", "abc")
    assert (revoke.command, revoke.environment, revoke.data_dir) == ("env revoke", "abc", tmp_path)
    assert parse("db", "switch", "/x").command == "db switch"
    update = parse("db", "update", "--check")
    assert (update.command, update.check, update.index) == ("db update", True, None)
    # The names before 2026-10-14 are gone (2026-10-14: hardly used yet).
    for old in (["pair-code"], ["envs"], ["db-switch", "/x"], ["db-update"]):
        with pytest.raises(SystemExit):
            parse(*old)
    with pytest.raises(SystemExit):
        parse("env", "revoke")  # the environment ID is required


def test_every_command_says_what_it_does(capsys):
    with pytest.raises(SystemExit):
        cli.main(["db", "--help"])
    help = capsys.readouterr().out
    assert "switch" in help and "validate a local DB folder" in help and "update" in help


def test_version(capsys):
    with pytest.raises(SystemExit) as exit:
        cli.main(["--version"])
    assert exit.value.code == 0 and capsys.readouterr().out.startswith("octantcompass ")


def test_gui_opens_the_window_before_reading_the_settings(tmp_path, monkeypatch):
    # A settings file that keeps OctantCompass from starting is fixed in the window, so the window opens all the same.
    (tmp_path / "settings.json").write_text('{"port": "not a port"}')
    seen = []
    monkeypatch.setattr(cli, "run_window", lambda places, manager: seen.append(
        (places.data, manager.serve_arguments, type(manager.local.engine).__name__, manager.local.given)) or 0)
    assert cli.main(["gui", "--data-dir", str(tmp_path)]) == 0
    # The host options for the start from the window; the window's DB section uses the same engine and DB folder.
    assert cli.main(["gui", "--fake-engine", "--port", "0", "--db", str(tmp_path / "other"),
                     "--data-dir", str(tmp_path)]) == 0
    assert seen == [(tmp_path, [], "OctantVectorEngine", None),
                    (tmp_path, ["--db", str(tmp_path / "other"), "--port", "0", "--fake-engine"], "FakeVectorEngine",
                     str(tmp_path / "other"))]
    # The language is chosen in the window (OctantPath Desktop no longer opens it).
    with pytest.raises(SystemExit):
        cli.main(["gui", "--lang", "ja", "--data-dir", str(tmp_path)])


def test_gui_says_when_no_window_can_be_shown(tmp_path, monkeypatch, capsys):
    def unavailable(places, manager):
        raise cli.WindowUnavailable("synthetic")
    monkeypatch.setattr(cli, "run_window", unavailable)
    with pytest.raises(SystemExit) as exit:
        cli.main(["gui", "--data-dir", str(tmp_path)])
    assert exit.value.code == 1 and "window could not be opened: synthetic" in capsys.readouterr().err
