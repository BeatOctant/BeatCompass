import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__, build
from .api import create_app
from .config import Config, LiveConfig
from .engine import DBError, EngineIncompatibleError, EngineState, EngineUnavailableError
from .dbupdate import DatabaseRequests, DatabaseSelection, DatabaseUpdater, LocalDatabase, db_in_use
from .manager import Manager
from .shortcut import make_shortcut, set_start_at_login
from .window import WindowUnavailable, run as run_window
from .engine.octantvector import OctantVectorEngine
from .engine.fake import FakeVectorEngine
from .lifecycle import serve
from .pairing import Pairing
from .places import Places


def build_parser():
    """Commands are a noun and a verb (`db switch`), as `git remote add`; each says what it does in --help."""
    parser = argparse.ArgumentParser(prog="octantcompass", description=(
        "OctantCompass: the recommendation host that OctantPath connects to. With no command on a terminal this help is "
        "shown; a double-click (no terminal) opens the window, like `gui`."))
    parser.add_argument("--version", action="version", version=version())
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path,
                        help="keep every file in this one folder (default: on Linux the XDG folders "
                             "~/.config, ~/.local/state and ~/.local/share, each with OctantCompass; elsewhere "
                             "the user's application data folder)")
    host = argparse.ArgumentParser(add_help=False, parents=[common])
    host.add_argument("--db", help="the DB folder to use at this start, over the adopted one")
    host.add_argument("--host", help="the address to listen on, over the setting")
    host.add_argument("--port", type=int, help="the port to listen on, over the setting (0: any free port)")
    host.add_argument("--fake-engine", action="store_true",
                      help="use a synthetic DB instead of OctantVector for development")
    commands = parser.add_subparsers(title="commands", metavar="<command>", required=True)

    def add(under, name, help, parents=(common,), **defaults):
        command = under.add_parser(name, parents=list(parents), help=help, description=help)
        command.set_defaults(**defaults)
        return command

    def group(name, help):
        # No --data-dir here: a subcommand's default would overwrite a value given before it.
        return add(commands, name, help, ()).add_subparsers(title="commands", metavar="<command>", required=True)

    add(commands, "gui", "open the OctantCompass window; start OctantCompass first when it is not running (with the "
        "host options given here)", (host,), command="gui")
    add(commands, "serve", "run OctantCompass in the foreground without a window, until it is stopped (Ctrl+C, "
        "SIGTERM, or `systemctl --user stop octantcompass` when it starts at login)", (host,), command="serve")
    pair = group("pair", "pair a OctantPath")
    add(pair, "code", "print a short single-use code to enter in OctantPath", command="pair code")
    env = group("env", "the paired environments (OctantPath)")
    add(env, "list", "print the paired environments as JSON", command="env list")
    add(env, "revoke", "revoke a paired environment",
        command="env revoke").add_argument("environment", help="the environment ID (from `env list`)")
    db = group("db", "the DB recommendations are made from")
    add(db, "switch", "validate a local DB folder and ask the host to switch to it (a stopped host at its next start)",
        command="db switch").add_argument("path", type=Path, help="the DB folder")
    add(db, "import", "check and extract a DB archive (<dbVersion>.full.tar) and ask the host to switch to it",
        command="db import").add_argument("path", type=Path, help="the DB archive")
    update = add(db, "update", "fetch a newer DB from the index and ask the host to switch to it", command="db update")
    update.add_argument("--index", help="index https URL; the setting index_url when omitted")
    update.add_argument("--check", action="store_true", help="only show what would be fetched")
    add(commands, "shortcut", "add the window to the applications menu (Linux) or the Start menu (Windows)",
        command="shortcut")
    return parser


def version():
    return f"octantcompass {__version__}" + (f" ({build.commit()})" if build.commit() else "")


def launched_without_terminal():
    """Started by a double-click rather than from a terminal: no terminal on stdin. On Windows the executable gets a
    console either way, so a start without a command always opens the window there."""
    return sys.platform == "win32" or sys.stdin is None or not sys.stdin.isatty()


def main(argv=None):
    parser = build_parser()
    arguments = sys.argv[1:] if argv is None else list(argv)
    if not arguments:
        if not launched_without_terminal():
            parser.print_help()
            return 2
        arguments = ["gui"]
    args = parser.parse_args(arguments)
    places = Places.at(args.data_dir) if args.data_dir is not None else Places.default()
    if args.command == "gui":
        # Before the settings are read: the window is where a setting that keeps OctantCompass from starting is fixed.
        engine = FakeVectorEngine() if args.fake_engine else OctantVectorEngine()
        try:
            return run_window(places, Manager(places, serve_arguments(args), engine, args.db))
        except (OSError, WindowUnavailable) as exc:
            parser.exit(1, f"the OctantCompass window could not be opened: {exc}\n")
    try:
        config = Config.load(places, **{key: getattr(args, key, None) for key in ("db", "host", "port")})
        selection = DatabaseSelection(places.state)
        requests = DatabaseRequests(places.state)
        # The `db` commands do what the window's DB section does: the new folder is handed to the running host (or
        # the next start) as a switch request.
        if args.command == "db switch":
            print(json.dumps(LocalDatabase(places, OctantVectorEngine()).switch(args.path)))
            return 0
        if args.command == "db update":
            return update_db(config, args)
        if args.command == "db import":
            print(json.dumps(LocalDatabase(places, OctantVectorEngine()).import_file(args.path, config)))
            return 0
        if args.command == "shortcut":
            print(make_shortcut(places))
            return 0
        serving = args.command == "serve"
        selected = selection.read() if serving else None
        if serving and args.db is None:
            config = replace(config, db=db_in_use(config, selection))
        pairing = Pairing(places.pairing)
        if args.command == "pair code":
            print(pairing.issue_code(digits=config.pair_code_digits, seconds=config.pair_code_seconds,
                                     attempts=config.pair_code_attempts))
            return 0
        if args.command == "env revoke":
            if not pairing.revoke(args.environment):
                return 1
            return 0
        if args.command == "env list":
            print(json.dumps(pairing.environments(), ensure_ascii=False))
            return 0
        if config.allow_unsigned_db:
            print("warning: allow_unsigned_db is on: DB updates without a signature are used without a warning (development only)",
                  file=sys.stderr)
        if config.start_at_login:
            # Write the login entry again, so it still points at this executable after it was moved or replaced.
            try:
                set_start_at_login(places, True)
            except (OSError, RuntimeError, KeyError) as exc:
                print(f"warning: the login entry could not be refreshed ({exc})", file=sys.stderr)
        engine = FakeVectorEngine() if args.fake_engine else OctantVectorEngine()
        state = EngineState(engine)
        updater = DatabaseUpdater(state)
        initial_request = requests.read() if args.db is not None else selected
        # The window changes settings while this runs; command-line values keep winning.
        live = LiveConfig(config, {key: getattr(args, key) for key in ("db", "host", "port")})
        return asyncio.run(serve(live, lambda: create_app(live, pairing, state,
                             watch_db=lambda: updater.watch(requests, initial_request, lambda: live.current.poll_seconds,
                                                            selection))))
    except (OSError, ValueError, RuntimeError, DBError, EngineIncompatibleError, EngineUnavailableError) as exc:
        # The window shows this line when a start from it fails: say what stopped it.
        parser.exit(1, f"OctantCompass could not start or complete the command: {exc}\n")


def update_db(config, args):
    index = args.index or config.index_url
    if not index:
        print("no index: pass --index or set index_url in settings.json", file=sys.stderr)
        return 1
    # The DB the host uses (its adopted selection, else the configured folder) gives the local version.
    local = LocalDatabase(config.places, OctantVectorEngine())
    folder = local.in_use(config)
    print(json.dumps(local.check(index, config, folder) if args.check else local.update(index, config, folder)))
    return 0


def serve_arguments(args):
    """The host options given to `gui`, for the `serve` the window starts."""
    given = [(f"--{key}", getattr(args, key)) for key in ("db", "host", "port")]
    return [part for option, value in given if value is not None for part in (option, str(value))] + \
        (["--fake-engine"] if args.fake_engine else [])


if __name__ == "__main__":
    raise SystemExit(main())
