import io
import json
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from octantcompass import __version__, cli, dbupdate
from octantcompass.config import Config
from octantcompass.manager import Manager, Refused
from octantcompass.places import Places
from octantcompass.dbupdate import USER_AGENT, DatabaseRequests, DatabaseSelection, DatabaseUpdater, NotHttps, UpdateRunning
from octantcompass.engine import DBError, EngineState, SignatureWarning, VersionError
from octantcompass.shortcut import exec_quote, make_shortcut

from test_dbupdate import LocalEngine


LIMIT = Config(".").index_max_bytes  # the default index_max_megabytes
HOST = "https://db.test/"


class Seen(list):
    pass


class Served(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


@pytest.fixture(autouse=True)
def served(tmp_path, monkeypatch):
    """The index and its parts are fetched over https only, so the tests serve tmp_path at HOST in place of urlopen.
    -> the (URL, User-Agent) of each request; `redirects` maps a URL to where its response says it ended up."""
    seen = Seen()
    seen.redirects = {}

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("User-agent")))
        assert request.full_url.startswith(HOST)
        data = (tmp_path / request.full_url[len(HOST):]).read_bytes()  # a missing file raises OSError
        return Served(data, seen.redirects.get(request.full_url, request.full_url))

    monkeypatch.setattr(dbupdate, "urlopen", urlopen)
    return seen


def url(path, root):
    return HOST + Path(path).relative_to(root).as_posix()


class IndexEngine(LocalEngine):
    """Contract 1 (OctantCompass to OctantVector) plan_update/apply_update over a synthetic index: entries after the
    local version, and an applied DB folder named after the last version holding the fetched part contents. With
    `signature`, apply_update warns as the adapter passes on OctantVector's SignatureWarning."""

    signature = None

    def plan_update(self, index, local_version):
        if index.get("indexVersion") != "1.0":
            raise VersionError("synthetic")
        versions = [e["dbVersion"] for e in index["entries"]]
        if versions[-1] == local_version:
            return []
        return index["entries"][versions.index(local_version) + 1:] if local_version in versions else index["entries"]

    def apply_update(self, files, base, out, trust=None):
        self.calls.append(("apply_update", [(e["dbVersion"], [Path(p).read_text() for p in parts]) for e, parts in files],
                           base, out, trust))
        if self.error:
            raise self.error
        if self.signature:
            warnings.warn(SignatureWarning(self.signature))
        Path(out).mkdir(parents=True)
        return Path(out)

    def import_archive(self, path, out, trust=None):
        self.calls.append(("import_archive", path, out, trust))
        if self.error:
            raise self.error
        if self.signature:
            warnings.warn(SignatureWarning(self.signature))
        Path(out).mkdir(parents=True)
        return Path(out)


def publish(folder, versions):
    """A synthetic index next to its parts: the first version full, the rest diffs; parts carry no URL."""
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for n, version in enumerate(versions):
        names = [f"{version}.tar.part{m}" for m in range(2)]
        for name in names:
            (folder / name).write_text(f"{name} bytes")
        entries.append({"dbVersion": version, "schemaVersion": "1.0", "kind": "diff" if n else "full",
                        "parts": [{"name": name, "size": len(f"{name} bytes"), "sha256": "sha256:synthetic", "url": None}
                                  for name in names]})
    (folder / "index.json").write_text(json.dumps({"indexVersion": "1.0", "entries": entries}))
    return folder / "index.json"


def test_update_fetches_the_plan_applies_and_switches(tmp_path):
    index = publish(tmp_path / "publish", ["v1", "v2", "v3"])
    engine = IndexEngine()
    state = EngineState(engine)
    state.load(tmp_path / "v1")
    selection = DatabaseSelection(tmp_path)
    updater = DatabaseUpdater(state)
    checked = updater.check(url(index, tmp_path), index_limit=LIMIT)
    assert [e["dbVersion"] for e in checked["entries"]] == ["v2", "v3"] and checked["current"] == "v1"
    info, signatures = updater.update(url(index, tmp_path), tmp_path / "data", {"keys": {}, "allowUnsigned": True}, selection,
                                      index_limit=LIMIT)
    assert info["dbVersion"] == "v3" and signatures == []
    _, files, base, out, trust = next(c for c in engine.calls if c[0] == "apply_update")
    assert files == [("v2", ["v2.tar.part0 bytes", "v2.tar.part1 bytes"]), ("v3", ["v3.tar.part0 bytes", "v3.tar.part1 bytes"])]
    assert base == tmp_path / "v1" and out == tmp_path / "data" / "dbs" / "v3" and trust["allowUnsigned"] is True
    assert state.snapshot.path == out and selection.read()["path"] == str(out)
    assert list((tmp_path / "data" / "downloads").iterdir()) == []
    # Already the newest.
    assert updater.update(url(index, tmp_path), tmp_path / "data", None, selection, index_limit=LIMIT) == (None, [])


def test_full_needs_no_base_and_failures_keep_the_current_db(tmp_path):
    index = publish(tmp_path / "publish", ["v1"])
    engine = IndexEngine()
    state = EngineState(engine)
    state.load(tmp_path / "old")
    updater = DatabaseUpdater(state)
    engine.error = DBError("synthetic signature failure")
    old = state.snapshot
    with pytest.raises(DBError):
        updater.update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert state.snapshot is old and state.update_reason == "db_update_failed" and updater.progress is None
    assert list((tmp_path / "data" / "downloads").iterdir()) == []
    assert next(c for c in engine.calls if c[0] == "apply_update")[2] is None
    with pytest.raises(OSError):
        updater.check(url(tmp_path / "missing" / "index.json", tmp_path), index_limit=LIMIT)
    (tmp_path / "future.json").write_text(json.dumps({"indexVersion": "9.0", "entries": []}))
    with pytest.raises(VersionError):
        updater.check(url(tmp_path / "future.json", tmp_path), index_limit=LIMIT)
    assert state.update_reason == "index_unsupported"


def test_one_update_at_a_time(tmp_path):
    updater = DatabaseUpdater(EngineState(IndexEngine()))
    updater.running.acquire()
    with pytest.raises(UpdateRunning):
        updater.update(str(tmp_path / "index.json"), tmp_path, None, index_limit=LIMIT)


def window_for(data, engine, **settings):
    """The window's side (manager.py) of a OctantCompass kept in data, with these settings."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps({key: str(value) for key, value in settings.items()}))
    return Manager(Places.at(data), engine=engine)


def refused(call, *args):
    with pytest.raises(Refused) as refusal:
        call(*args)
    return refusal.value.code


def test_the_window_checks_and_imports_from_the_index(tmp_path):
    """The window updates the DB in use itself, whether the host runs or not, and hands the new one over as the
    `db` commands do (2026-10-19)."""
    index = publish(tmp_path / "publish", ["v1", "v2"])
    data = tmp_path / "data"
    manager = window_for(data, IndexEngine(), db=tmp_path / "v1", index_url=url(index, tmp_path))
    assert manager.status()["db"]["indexUrl"] == url(index, tmp_path)
    checked = manager.check_update()
    assert checked["dbVersion"] == "v2" and checked["current"] == "v1" and "plan" not in checked
    assert refused(manager.check_update, url(tmp_path / "none.json", tmp_path)) == "index_unreadable"
    assert refused(manager.check_update, str(index)) == "index_not_https"
    assert refused(manager.update_db, "http://db.test/publish/index.json") == "index_not_https"
    assert manager.update_db() == {"updated": True, "dbVersion": "v2", "signatureWarnings": []}
    # Handed to the host as a request: until it adopts it, v1 stays in use and the next update plans from v1.
    assert DatabaseRequests(data).read()["path"] == str(data / "dbs" / "v2")
    db = manager.status()["db"]
    assert db["path"] == str(tmp_path / "v1") and db["pending"] == str(data / "dbs" / "v2")
    DatabaseSelection(data).publish(data / "dbs" / "v2", DatabaseRequests(data).read()["revision"])  # the host adopts it
    assert manager.status()["db"]["pending"] is None
    assert manager.update_db() == {"updated": False, "dbVersion": "v2", "signatureWarnings": []}
    assert refused(window_for(tmp_path / "bare", IndexEngine()).update_db) == "no_index"


def test_the_window_updates_the_db_the_running_host_uses(tmp_path):
    """The DB section shows the running host's DB, and an update plans from that one, not from the folder a start
    from here would use (--db, the adopted one or the setting)."""
    index = publish(tmp_path / "publish", ["v1", "v2"])
    manager = window_for(tmp_path / "data", IndexEngine(), db=tmp_path / "v1", index_url=url(index, tmp_path))
    manager.host = lambda path, body=None: (200, {"dbPath": str(tmp_path / "v2")})
    assert manager.status()["db"]["path"] == str(tmp_path / "v2")
    assert manager.check_update()["current"] == "v2"
    assert manager.update_db() == {"updated": False, "dbVersion": "v2", "signatureWarnings": []}


def test_an_update_is_cancelled_while_its_parts_are_fetched(tmp_path, monkeypatch):
    index = publish(tmp_path / "publish", ["v1", "v2"])
    data = tmp_path / "data"
    engine = IndexEngine()
    manager = window_for(data, engine, db=tmp_path / "v1", index_url=url(index, tmp_path))
    fetch = dbupdate.fetch

    def fetch_then_cancel(*args, **options):
        assert manager.local.running()["progress"]["done"] == 0
        assert manager.cancel_update() == {"cancelled": True}
        fetch(*args, **options)
    monkeypatch.setattr(dbupdate, "fetch", fetch_then_cancel)
    assert refused(manager.update_db) == "update_cancelled"
    # Nothing is applied or handed over, and the fetched parts are gone.
    assert not any(call[0] == "apply_update" for call in engine.calls) and DatabaseRequests(data).read() is None
    assert list((data / "downloads").iterdir()) == [] and manager.status()["db"]["work"] is None


def test_cli_db_update_hands_the_new_db_to_the_host(tmp_path, monkeypatch, capsys):
    index = publish(tmp_path / "publish", ["v1", "v2"])
    monkeypatch.setattr(cli, "OctantVectorEngine", IndexEngine)
    data = tmp_path / "data"
    DatabaseSelection(data).publish(tmp_path / "v1")
    assert cli.main(["db", "update", "--data-dir", str(data)]) == 1  # no index configured
    assert cli.main(["db", "update", "--check", "--index", url(index, tmp_path), "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["dbVersion"] == "v2"
    assert cli.main(["db", "update", "--index", url(index, tmp_path), "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out) == {"updated": True, "dbVersion": "v2", "signatureWarnings": []}
    assert DatabaseRequests(data).read()["path"] == str(data / "dbs" / "v2")


def test_signature_warnings_reach_the_window_and_the_cli(tmp_path, monkeypatch, capsys, caplog):
    """OctantVector uses a version not signed by a known key and only warns (OctantSpecs contracts/distribution.md, 署名).
    The warning is what tells the user, so it is in the window's answer and `db update`'s output."""
    index = publish(tmp_path / "publish", ["v1", "v2"])
    engine = IndexEngine()
    engine.signature = "v2: synthetic/v2: no manifest.sig"
    manager = window_for(tmp_path / "window", engine, db=tmp_path / "v1", index_url=url(index, tmp_path))
    assert manager.update_db() == {
        "updated": True, "dbVersion": "v2", "signatureWarnings": ["v2: synthetic/v2: no manifest.sig"]}
    monkeypatch.setattr(IndexEngine, "signature", "v2: synthetic/v2: signed by key abcd, which this OctantVector does not know")
    monkeypatch.setattr(cli, "OctantVectorEngine", IndexEngine)
    data = tmp_path / "data"
    DatabaseSelection(data).publish(tmp_path / "v1")
    assert cli.main(["db", "update", "--index", url(index, tmp_path), "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["signatureWarnings"] == [IndexEngine.signature]
    assert IndexEngine.signature in caplog.text  # on stderr, where no handler is set up


def test_no_command_opens_the_window(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "run_window", lambda places, manager: seen.append(places.data) or 0)
    monkeypatch.setattr(sys, "argv", ["octantcompass"])
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(cli.Places, "default", classmethod(lambda cls: cls.at(tmp_path)))
    assert cli.main([]) == 0 and seen == [tmp_path]


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the Linux applications entry")
def test_linux_shortcut(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    path = make_shortcut(tmp_path / "data dir")
    text = path.read_text()
    assert path == tmp_path / "share" / "applications" / "octantcompass.desktop"
    assert f"Exec={sys.executable} -m octantcompass gui --data-dir \"{tmp_path / 'data dir'}\"" in text
    # The window names itself octantcompass (window.py): the desktop shows it under this entry.
    assert "StartupWMClass=octantcompass\n" in text
    assert exec_quote("a$b") == '"a\\\\$b"' and exec_quote("50%") == "50%%"


@pytest.mark.parametrize("version", ["../x", "a/b", "a\\b", "..", ".", "/abs", "", "C:x", "a\0b", "a" * 300, None])
def test_unsafe_db_folder_names_are_refused(tmp_path, version):
    """The index is unsigned, so its dbVersion must not lead the new DB folder out of dbs/."""
    index = publish(tmp_path / "publish", ["v1"])
    raw = json.loads(index.read_text())
    raw["entries"][0]["dbVersion"] = version
    index.write_text(json.dumps(raw))
    engine = IndexEngine()
    state = EngineState(engine)
    state.load(tmp_path / "old")
    updater = DatabaseUpdater(state)
    with pytest.raises(ValueError, match="unsafe DB folder"):
        updater.update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert state.update_reason == "db_update_failed" and not any(c[0] == "apply_update" for c in engine.calls)
    assert not (tmp_path / "data").exists()  # refused at plan, before any folder is made


def test_a_link_in_dbs_does_not_lead_the_new_db_out(tmp_path):
    index = publish(tmp_path / "publish", ["v2"])
    (tmp_path / "data" / "dbs").mkdir(parents=True)
    (tmp_path / "data" / "dbs" / "v2").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    engine = IndexEngine()
    state = EngineState(engine)
    state.load(tmp_path / "old")
    with pytest.raises(ValueError, match="unsafe DB folder"):
        DatabaseUpdater(state).update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert not any(c[0] == "apply_update" for c in engine.calls) and not (tmp_path / "elsewhere").exists()


def test_a_dotted_version_names_the_folder(tmp_path):
    index = publish(tmp_path / "publish", ["2026.10.14"])
    state = EngineState(IndexEngine())
    state.load(tmp_path / "old")
    info, _ = DatabaseUpdater(state).update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert state.snapshot.path == tmp_path / "data" / "dbs" / "2026.10.14" and info is not None


def test_the_index_and_parts_are_asked_for_by_name_within_their_sizes(tmp_path, served):
    """Every request names OctantCompass and a contact; the index is read up to index_max_megabytes, and each part up
    to the size the index gives, since the parts are checked only after they arrive."""
    index = publish(tmp_path / "publish", ["v1", "v2"])
    state = EngineState(IndexEngine())
    state.load(tmp_path / "old")
    info, _ = DatabaseUpdater(state).update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert info["dbVersion"] == "v2" and len(served) == 5  # the index and two parts of each version
    assert {agent for _, agent in served} == {USER_AGENT}
    assert USER_AGENT == f"OctantCompass/{__version__} ( https://bsky.app/profile/beatoctant.bsky.social )"
    with pytest.raises(ValueError, match="index is larger"):
        DatabaseUpdater(state).check(url(index, tmp_path), index_limit=len(index.read_bytes()) - 1)


@pytest.mark.parametrize("where", ["http://db.test/publish/index.json", "file:///publish/index.json", "/publish/index.json",
                                   "publish/index.json", "HTTP://db.test/publish/index.json"])
def test_an_index_not_over_https_is_refused_before_it_is_asked_for(tmp_path, served, where):
    """The index is not signed and gives the parts' hashes, so over anything but https it and the parts could be
    swapped on the way (OctantSpecs contracts/distribution.md, 取得の決まり. 2026-10-16)."""
    publish(tmp_path / "publish", ["v1"])
    state = EngineState(IndexEngine())
    state.load(tmp_path / "old")
    with pytest.raises(NotHttps):
        DatabaseUpdater(state).check(where, index_limit=LIMIT)
    assert served == [] and state.update_reason == "db_update_failed"


@pytest.mark.parametrize("part", ["http://db.test/publish/v1.tar.part1", "file:///etc/hostname"])
def test_a_part_not_over_https_is_refused_before_anything_is_fetched(tmp_path, served, part):
    index = publish(tmp_path / "publish", ["v1"])
    raw = json.loads(index.read_text())
    raw["entries"][0]["parts"][1]["url"] = part
    index.write_text(json.dumps(raw))
    engine = IndexEngine()
    state = EngineState(engine)
    state.load(tmp_path / "old")
    with pytest.raises(NotHttps):
        DatabaseUpdater(state).update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert [u for u, _ in served] == [url(index, tmp_path)] and not any(c[0] == "apply_update" for c in engine.calls)
    assert state.update_reason == "db_update_failed"


def test_a_redirect_away_from_https_is_refused(tmp_path, served):
    index = publish(tmp_path / "publish", ["v1"])
    state = EngineState(IndexEngine())
    state.load(tmp_path / "old")
    served.redirects[url(index, tmp_path)] = "http://db.test/publish/index.json"
    with pytest.raises(NotHttps):
        DatabaseUpdater(state).check(url(index, tmp_path), index_limit=LIMIT)
    served.redirects.clear()
    served.redirects[url(tmp_path / "publish" / "v1.tar.part0", tmp_path)] = "http://db.test/publish/v1.tar.part0"
    with pytest.raises(NotHttps):
        DatabaseUpdater(state).update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert list((tmp_path / "data" / "downloads").iterdir()) == []


def test_a_part_larger_than_the_index_says_is_refused_while_it_arrives(tmp_path):
    index = publish(tmp_path / "publish", ["v1"])
    raw = json.loads(index.read_text())
    raw["entries"][0]["parts"][1]["size"] -= 1
    index.write_text(json.dumps(raw))
    engine = IndexEngine()
    state = EngineState(engine)
    state.load(tmp_path / "old")
    updater = DatabaseUpdater(state)
    with pytest.raises(ValueError, match="larger than the"):
        updater.update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert state.update_reason == "db_update_failed" and not any(c[0] == "apply_update" for c in engine.calls)
    assert list((tmp_path / "data" / "downloads").iterdir()) == []
    # A part without a size is refused before anything is fetched.
    del raw["entries"][0]["parts"][1]["size"]
    index.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="no size"):
        updater.check(url(index, tmp_path), index_limit=LIMIT)


def test_an_update_larger_than_the_free_disk_is_refused_before_anything_is_fetched(tmp_path, monkeypatch, caplog):
    """The sizes bound each part, but the index giving them is not signed: one that gives more than the disk holds
    is refused before the first part, so it cannot fill the disk."""
    index = publish(tmp_path / "publish", ["v1"])
    size = sum(part["size"] for part in json.loads(index.read_text())["entries"][0]["parts"])
    fetched = []
    fetch = dbupdate.fetch
    monkeypatch.setattr(dbupdate, "fetch", lambda url, *args: fetched.append(url))
    monkeypatch.setattr(dbupdate.shutil, "disk_usage", lambda folder: SimpleNamespace(free=size * dbupdate.ROOM_FACTOR - 1))
    engine = IndexEngine()
    state = EngineState(engine)
    state.load(tmp_path / "old")
    with pytest.raises(ValueError, match="free"):
        DatabaseUpdater(state).update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert fetched == [] and not any(c[0] == "apply_update" for c in engine.calls)
    assert state.update_reason == "db_update_failed" and "bytes free" in caplog.text
    assert list((tmp_path / "data" / "downloads").iterdir()) == []
    # Exactly the room the update needs (the parts, the joined tar and the extracted files) is enough.
    monkeypatch.setattr(dbupdate, "fetch", fetch)
    monkeypatch.setattr(dbupdate.shutil, "disk_usage", lambda folder: SimpleNamespace(free=size * dbupdate.ROOM_FACTOR))
    info, _ = DatabaseUpdater(state).update(url(index, tmp_path), tmp_path / "data", None, index_limit=LIMIT)
    assert info["dbVersion"] == "v1"


def test_a_handed_over_archive_is_imported_into_the_data_folder_and_switched_to(tmp_path):
    """The Dev 版 hand-off (OctantSpecs contracts/distribution.md, 非公開（探求中）の扱い): one <dbVersion>.full.tar, checked
    and extracted by OctantVector's import_archive into data/dbs, named after the file."""
    tar = tmp_path / "hand" / "v5.full.tar"
    tar.parent.mkdir()
    tar.write_bytes(b"synthetic")
    engine = IndexEngine()
    data = tmp_path / "data"
    manager = window_for(data, engine, db=tmp_path / "v1")
    assert refused(manager.import_db, "v5.full.tar") == "path_not_absolute"
    assert manager.import_db(str(tar)) == {"dbVersion": "v5", "signatureWarnings": []}
    _, path, out, trust = next(c for c in engine.calls if c[0] == "import_archive")
    assert path == tar and out == data / "dbs" / "v5" and trust == {"keys": {}, "allowUnsigned": False}
    assert DatabaseRequests(data).read()["path"] == str(out) and manager.status()["db"]["pending"] == str(out)
    # The same file again goes next to the first, which stays.
    assert manager.import_db(str(tar))["dbVersion"].startswith("v5-")
    engine.error = DBError("synthetic: does not match the manifest")
    assert refused(manager.import_db, str(tar)) == "db_import_failed"
    assert DatabaseRequests(data).read()["path"].startswith(str(out) + "-")
    engine.error = None
    bad = tmp_path / "hand" / ".full.tar"  # no name left for the folder
    bad.write_bytes(b"synthetic")
    assert refused(manager.import_db, str(bad)) == "db_import_failed"
    assert refused(manager.import_db, str(tmp_path / "none.tar")) == "db_import_failed"
    (data / "settings.json").write_text("{")
    assert refused(manager.import_db, str(tar)) == "settings_unreadable"


def test_cli_db_import_hands_the_new_db_to_the_host_with_its_signature_warnings(tmp_path, monkeypatch, capsys):
    tar = tmp_path / "2026.10.15.full.tar"
    tar.write_bytes(b"synthetic")
    monkeypatch.setattr(IndexEngine, "signature", "2026.10.15: no manifest.sig")
    monkeypatch.setattr(cli, "OctantVectorEngine", IndexEngine)
    data = tmp_path / "data"
    assert cli.main(["db", "import", str(tar), "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out) == {"dbVersion": "2026.10.15",
                                                   "signatureWarnings": ["2026.10.15: no manifest.sig"]}
    assert DatabaseRequests(data).read()["path"] == str(data / "dbs" / "2026.10.15")


def test_an_import_waits_for_no_update(tmp_path):
    updater = DatabaseUpdater(EngineState(IndexEngine()))
    updater.running.acquire()
    with pytest.raises(UpdateRunning):
        updater.import_file(tmp_path / "v1.full.tar", tmp_path, None)
