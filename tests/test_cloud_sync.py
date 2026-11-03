"""Keeping the SQLite database in a bucket object: download, upload with a version check, the update job and the page."""

from pathlib import Path

import pytest

from valuation_monitor import cloud
from valuation_monitor.cloud import ConcurrentUpdate, RemoteDatabase
from valuation_monitor.web import create_app


class FakeStore:
    """A bucket in memory: objects have a generation that grows with every upload, like GCS."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.next = 1000
        self.uploads = 0

    def put(self, uri, data):
        self.next += 1
        self.objects[uri] = (data, self.next)

    def generation(self, uri):
        return self.objects[uri][1] if uri in self.objects else 0

    def download(self, uri, path):
        data, gen = self.objects[uri]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return gen

    def upload(self, uri, path, if_generation_match):
        if self.generation(uri) != if_generation_match:
            raise ConcurrentUpdate(f"{uri} was changed by someone else since it was downloaded; nothing was uploaded")
        self.put(uri, path.read_bytes())
        self.uploads += 1
        return self.objects[uri][1]


URI = "gs://bucket/valuation.db"


def test_pull_downloads_only_when_the_object_is_a_newer_version(tmp_path):
    store = FakeStore()
    store.put(URI, b"v1")
    remote = RemoteDatabase(URI, tmp_path / "local" / "valuation.db", store)
    assert remote.pull() is True and (tmp_path / "local" / "valuation.db").read_bytes() == b"v1"
    assert remote.pull() is False  # the same version: nothing is downloaded again
    store.put(URI, b"v2")
    assert remote.pull() is True and (tmp_path / "local" / "valuation.db").read_bytes() == b"v2"


def test_pull_without_an_object_keeps_the_local_file_and_the_first_push_creates_it(tmp_path):
    store = FakeStore()
    local = tmp_path / "valuation.db"
    local.write_bytes(b"seed")
    remote = RemoteDatabase(URI, local, store)
    assert remote.pull() is False and local.read_bytes() == b"seed"
    local.write_bytes(b"seed plus a change")
    assert remote.push() is True and store.objects[URI][0] == b"seed plus a change"


def test_push_uploads_only_what_changed_since_the_pull(tmp_path):
    store = FakeStore()
    store.put(URI, b"v1")
    local = tmp_path / "valuation.db"
    remote = RemoteDatabase(URI, local, store)
    remote.pull()
    assert remote.push() is False and store.uploads == 0  # unchanged: no new version of the object
    local.write_bytes(b"v1 and more")
    assert remote.push() is True and store.uploads == 1
    assert remote.push() is False  # already uploaded
    assert remote.pull() is False  # our own upload is the current version


def test_push_refuses_to_overwrite_a_version_it_has_not_seen(tmp_path):
    store = FakeStore()
    store.put(URI, b"v1")
    local = tmp_path / "valuation.db"
    remote = RemoteDatabase(URI, local, store)
    remote.pull()
    store.put(URI, b"someone else was faster")  # another job uploaded in the meantime
    local.write_bytes(b"my update")
    with pytest.raises(ConcurrentUpdate):
        remote.push()
    assert store.objects[URI][0] == b"someone else was faster"  # nothing was lost
    assert remote.pull() is True and local.read_bytes() == b"someone else was faster"


def test_from_env_is_off_without_a_uri(monkeypatch, tmp_path):
    monkeypatch.delenv("VALUATION_DB_URI", raising=False)
    assert RemoteDatabase.from_env(tmp_path / "x.db") is None
    monkeypatch.setenv("VALUATION_DB_URI", URI)
    assert RemoteDatabase.from_env(tmp_path / "x.db", FakeStore()).uri == URI
    with pytest.raises(ValueError):
        RemoteDatabase("not-a-uri", tmp_path / "x.db")


def test_update_job_downloads_updates_and_uploads(monitor, tmp_path, monkeypatch, capsys):
    from valuation_monitor import cli

    store = FakeStore()
    store.put(URI, b"the old database")
    local = tmp_path / "job" / "valuation.db"
    monkeypatch.setenv("VALUATION_DB_URI", URI)
    monkeypatch.setattr(cloud.RemoteDatabase, "store", property(lambda self: store))

    def fake_monitor(*args, **kwargs):
        assert kwargs["db_path"] == local and local.read_bytes() == b"the old database"  # the job works on the downloaded copy
        local.write_bytes(b"the updated database")
        return monitor

    monkeypatch.setattr(cli, "Monitor", fake_monitor)
    monkeypatch.setattr(cli, "load_config", lambda path: monitor.config)
    monkeypatch.setattr(cli, "check_freshness", lambda db, config: [])  # the fixture's old price would be stale: freshness has its own tests
    assert cli.update_main(["--db", str(local), "--data-dir", str(tmp_path / "data")]) == 0
    out = capsys.readouterr().out
    assert "Database uploaded" in out and "downloaded" in out and "Tencent Holdings (0700.HK): updated" in out
    assert store.objects[URI][0] == b"the updated database"


def test_update_job_fails_loudly_when_another_job_uploaded_first(monitor, tmp_path, monkeypatch, capsys):
    from valuation_monitor import cli

    store = FakeStore()
    store.put(URI, b"v1")
    local = tmp_path / "valuation.db"
    monkeypatch.setenv("VALUATION_DB_URI", URI)
    monkeypatch.setattr(cloud.RemoteDatabase, "store", property(lambda self: store))

    def fake_monitor(*args, **kwargs):
        local.write_bytes(b"mine")
        store.put(URI, b"theirs")  # the other job finishes while this one is running
        return monitor

    monkeypatch.setattr(cli, "Monitor", fake_monitor)
    monkeypatch.setattr(cli, "load_config", lambda path: monitor.config)
    monkeypatch.setattr(cli, "check_freshness", lambda db, config: [])  # the fixture's old price would be stale: freshness has its own tests
    assert cli.update_main(["--db", str(local), "--data-dir", str(tmp_path)]) == 1
    assert "changed by someone else" in capsys.readouterr().err and store.objects[URI][0] == b"theirs"


def test_update_job_without_a_bucket_works_on_a_local_file(monitor, tmp_path, monkeypatch, capsys):
    from valuation_monitor import cli

    monkeypatch.delenv("VALUATION_DB_URI", raising=False)
    monkeypatch.setattr(cli, "Monitor", lambda *a, **k: monitor)
    monkeypatch.setattr(cli, "load_config", lambda path: monitor.config)
    monkeypatch.setattr(cli, "check_freshness", lambda db, config: [])  # the fixture's old price would be stale: freshness has its own tests
    assert cli.update_main(["--db", str(tmp_path / "valuation.db")]) == 0
    assert "Database" not in capsys.readouterr().out.replace("Data directory", "")


def test_page_syncs_before_each_rebuild_and_never_while_reading_a_fresh_page(monitor):
    calls = []
    app = create_app(monitor, reload_seconds=3600, cached_load_offline=True, sync=lambda: calls.append("sync"))
    client = app.test_client()
    assert client.get("/").status_code == 200 and client.get("/stock/0700.HK/").status_code == 200
    assert calls == ["sync"]  # once for the first load, none for the second request

    import valuation_monitor.web as web

    real = web.time.monotonic
    try:
        web.time.monotonic = lambda: real() + 7200
        client.get("/")
    finally:
        web.time.monotonic = real
    assert calls == ["sync", "sync"]


def test_update_button_starts_the_job_instead_of_fetching(monitor):
    started = []
    app = create_app(monitor, update_remotely=lambda: started.append(1))
    client = app.test_client()
    r = client.post("/refresh", data={"next": "/stock/0700.HK/"})
    assert started == [1] and r.status_code == 302 and r.headers["Location"] == "/stock/0700.HK/?updating=1"
    page = client.get("/?updating=1").get_data(as_text=True)
    assert "Update started" in page and "Update started" not in client.get("/").get_data(as_text=True)


def test_update_button_reports_a_job_that_could_not_be_started(monitor, capsys):
    def boom():
        raise PermissionError("no run.jobs.run")

    client = create_app(monitor, update_remotely=boom).test_client()
    r = client.post("/refresh", data={"next": "/"})
    assert r.headers["Location"] == "/?update_failed=1" and "no run.jobs.run" in capsys.readouterr().err
    assert "could not be started" in client.get("/?update_failed=1").get_data(as_text=True)


def test_start_job_posts_to_the_cloud_run_api():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"name": "operations/abc"}

    class Session:
        def post(self, url, json, timeout):
            self.url = url
            return Response()

    session = Session()
    job = "projects/p/locations/asia-east2/jobs/valuation-update"
    assert cloud.start_job(job, session) == "operations/abc"
    assert session.url == f"https://run.googleapis.com/v2/{job}:run"


def test_wsgi_app_pulls_the_database_and_wires_the_button(tmp_path, monkeypatch):
    store = FakeStore()
    store.put(URI, Path(__file__).parent.parent.joinpath("data", "valuation.db").read_bytes())  # a real database for the real app
    monkeypatch.setattr(cloud.RemoteDatabase, "store", property(lambda self: store))
    monkeypatch.setenv("VALUATION_DB_URI", URI)
    monkeypatch.setenv("VALUATION_DB_PATH", str(tmp_path / "db" / "valuation.db"))
    monkeypatch.setenv("VALUATION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VALUATION_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("VALUATION_UPDATE_JOB", "projects/p/locations/r/jobs/j")
    import importlib

    wsgi = importlib.import_module("valuation_monitor.web.wsgi")
    app = wsgi.make_app()
    assert (tmp_path / "db" / "valuation.db").exists()  # pulled from the "bucket" into the local path
    assert app.config["refresh"]


def test_gcs_object_store_maps_a_failed_precondition_to_a_concurrent_update(tmp_path):
    pytest.importorskip("google.cloud.storage")
    from google.api_core.exceptions import PreconditionFailed

    class FakeBlob:
        generation = 7
        uploaded_with = None

        def upload_from_filename(self, filename, content_type, if_generation_match):
            FakeBlob.uploaded_with = if_generation_match
            if if_generation_match != 7:
                raise PreconditionFailed("generation mismatch")

        def download_to_filename(self, filename, if_generation_match):
            Path(filename).write_bytes(b"downloaded")
            assert if_generation_match == 7

    class FakeBucket:
        def get_blob(self, name):
            return FakeBlob() if name == "valuation.db" else None

        def blob(self, name):
            return FakeBlob()

    class FakeClient:
        def bucket(self, name):
            assert name == "bucket"
            return FakeBucket()

    store = cloud.GcsObjectStore.__new__(cloud.GcsObjectStore)
    store._client = FakeClient()
    assert store.generation(URI) == 7 and store.generation("gs://bucket/missing.db") == 0
    path = tmp_path / "sub" / "valuation.db"
    assert store.download(URI, path) == 7 and path.read_bytes() == b"downloaded" and not (tmp_path / "sub" / "valuation.db.part").exists()
    with pytest.raises(FileNotFoundError):
        store.download("gs://bucket/missing.db", path)
    path.write_bytes(b"x")
    assert store.upload(URI, path, if_generation_match=7) == 7 and FakeBlob.uploaded_with == 7
    with pytest.raises(ConcurrentUpdate, match="changed by someone else"):
        store.upload(URI, path, if_generation_match=3)
