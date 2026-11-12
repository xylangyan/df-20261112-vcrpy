import stat
import threading

import pytest

from vcr.cassette_merge import CassetteMergeConflictError, ConflictPolicy
from vcr.persisters import filesystem as fs
from vcr.persisters.filesystem import (
    CassetteNotFoundError,
    FilesystemPersister,
    MergingFilesystemPersister,
    _atomic_write,
)
from vcr.request import Request
from vcr.serializers import jsonserializer, yamlserializer


@pytest.mark.parametrize(
    "cassette_path, serializer, expected_message",
    [
        # Old JSON cassettes are recognized as old-format and pointed at the
        # migration script.
        ("tests/fixtures/migration/old_cassette.json", jsonserializer, "run the migration script"),
        # Old YAML cassettes contain python object tags the safe loader
        # refuses, so they surface the unsupported-tag error instead.
        ("tests/fixtures/migration/old_cassette.yaml", yamlserializer, "problem loading the cassette"),
    ],
)
def test_load_cassette_with_old_cassettes(cassette_path, serializer, expected_message):
    with pytest.raises(ValueError) as excinfo:
        FilesystemPersister.load_cassette(cassette_path, serializer)
    assert expected_message in excinfo.exconly()


@pytest.mark.parametrize(
    "cassette_path, serializer",
    [
        ("tests/fixtures/migration/not_cassette.txt", jsonserializer),
        ("tests/fixtures/migration/not_cassette.txt", yamlserializer),
    ],
)
def test_load_cassette_with_invalid_cassettes(cassette_path, serializer):
    with pytest.raises(Exception) as excinfo:
        FilesystemPersister.load_cassette(cassette_path, serializer)
    assert "run the migration script" not in excinfo.exconly()


@pytest.mark.parametrize("content", ["", "\n"])
@pytest.mark.parametrize("serializer", [jsonserializer, yamlserializer])
def test_load_empty_cassette(tmpdir, serializer, content):
    cassette_path = tmpdir.join("empty_cassette")
    cassette_path.write(content)
    with pytest.raises(CassetteNotFoundError):
        FilesystemPersister.load_cassette(str(cassette_path), serializer)


def _request(name):
    return Request("GET", f"http://example.com/{name}", None, {})


def _response(body):
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": {},
        "body": {"string": body},
    }


def _cassette_dict(names):
    return {
        "requests": [_request(name) for name in names],
        "responses": [_response(f"body-{name}") for name in names],
    }


def _saved_names(path, serializer):
    requests, _ = FilesystemPersister.load_cassette(str(path), serializer)
    return [request.uri.rsplit("/", 1)[1] for request in requests]


@pytest.mark.parametrize("serializer", [jsonserializer, yamlserializer])
def test_save_cassette_is_atomic_and_leaves_no_tmp_files(tmpdir, serializer):
    cassette_path = tmpdir.join("cassette")
    FilesystemPersister.save_cassette(str(cassette_path), _cassette_dict(["a"]), serializer)
    assert cassette_path.check(file=1)
    assert not [f for f in tmpdir.listdir() if f.basename.endswith(".tmp")]
    # The file can be read back through the normal load path.
    assert _saved_names(cassette_path, serializer) == ["a"]


def test_atomic_write_keeps_the_old_file_when_writing_fails(tmpdir, monkeypatch):
    cassette_path = tmpdir.join("cassette")
    FilesystemPersister.save_cassette(str(cassette_path), _cassette_dict(["a"]), yamlserializer)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fs.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        _atomic_write(cassette_path, "partial write")
    # The previous cassette is still there and readable, no temp file lingers.
    assert _saved_names(cassette_path, yamlserializer) == ["a"]
    assert not [f for f in tmpdir.listdir() if f.basename.endswith(".tmp")]


def test_merging_persister_combines_appends_from_two_processes(tmpdir):
    cassette_path = tmpdir.join("cassette")
    persister = MergingFilesystemPersister
    persister.save_cassette(str(cassette_path), _cassette_dict(["a"]), yamlserializer)

    # Both processes read the same generation, then save different additions.
    persister.load_cassette(str(cassette_path), yamlserializer)
    persister.load_cassette(str(cassette_path), yamlserializer)
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "theirs"]), yamlserializer)
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "mine"]), yamlserializer)

    assert _saved_names(cassette_path, yamlserializer) == ["a", "theirs", "mine"]


def test_merging_persister_reports_a_rewrite_conflict(tmpdir):
    cassette_path = tmpdir.join("cassette")
    persister = MergingFilesystemPersister
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "b"]), yamlserializer)
    persister.load_cassette(str(cassette_path), yamlserializer)

    # Another process rewrites the response at position 1 in the file.
    current = yamlserializer.deserialize(cassette_path.read())
    current["interactions"][1]["response"]["body"]["string"] = "from-the-other-process"
    cassette_path.write(yamlserializer.serialize(current))

    mine = _cassette_dict(["a", "b"])
    mine["responses"][1]["body"]["string"] = b"from-this-process"
    with pytest.raises(CassetteMergeConflictError) as excinfo:
        persister.save_cassette(str(cassette_path), mine, yamlserializer)
    assert [position for position, *_ in excinfo.value.conflicts] == [1]
    # The file was not replaced by the conflicting save.
    assert cassette_path.read() == yamlserializer.serialize(current)


@pytest.mark.parametrize(
    "policy, expected_body",
    [("keep_theirs", "from-the-other-process"), ("keep_mine", "from-this-process")],
)
def test_merging_persister_conflict_policies(tmpdir, policy, expected_body):
    persister = FilesystemPersister.configure(safe_append=True, on_conflict=policy)
    cassette_path = tmpdir.join("cassette")
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "b"]), yamlserializer)
    persister.load_cassette(str(cassette_path), yamlserializer)

    current = yamlserializer.deserialize(cassette_path.read())
    current["interactions"][1]["response"]["body"]["string"] = "from-the-other-process"
    cassette_path.write(yamlserializer.serialize(current))

    mine = _cassette_dict(["a", "b"])
    mine["responses"][1]["body"]["string"] = b"from-this-process"
    persister.save_cassette(str(cassette_path), mine, yamlserializer)

    saved = yamlserializer.deserialize(cassette_path.read())
    assert saved["interactions"][1]["response"]["body"]["string"] == expected_body


def test_merging_persister_can_save_again_after_reloading(tmpdir):
    cassette_path = tmpdir.join("cassette")
    persister = MergingFilesystemPersister
    persister.save_cassette(str(cassette_path), _cassette_dict(["a"]), yamlserializer)

    # The first save merges an addition made in the meantime.
    persister.load_cassette(str(cassette_path), yamlserializer)
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "mine"]), yamlserializer)

    # Reload and keep appending: the new base is the just-merged file.
    requests, responses = persister.load_cassette(str(cassette_path), yamlserializer)
    assert [request.uri.rsplit("/", 1)[1] for request in requests] == ["a", "mine"]
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "mine", "more"]), yamlserializer)
    assert _saved_names(cassette_path, yamlserializer) == ["a", "mine", "more"]


def test_configure_rejects_unknown_conflict_policy():
    with pytest.raises(ValueError, match="not a valid conflict policy"):
        FilesystemPersister.configure(safe_append=True, on_conflict="shout")


def test_configured_persister_without_safe_append_overwrites_like_the_default(tmpdir):
    persister = FilesystemPersister.configure()
    assert persister.safe_append is False
    cassette_path = tmpdir.join("cassette")
    persister.save_cassette(str(cassette_path), _cassette_dict(["a"]), yamlserializer)
    persister.save_cassette(str(cassette_path), _cassette_dict(["b"]), yamlserializer)
    assert _saved_names(cassette_path, yamlserializer) == ["b"]


def test_merging_persister_works_with_a_custom_serializer(tmpdir):
    class WrappingSerializer:
        """A serializer that keeps the existing (de)serialize call convention."""

        @staticmethod
        def serialize(cassette_dict):
            return yamlserializer.serialize(cassette_dict)

        @staticmethod
        def deserialize(cassette_string):
            return yamlserializer.deserialize(cassette_string)

    persister = MergingFilesystemPersister
    cassette_path = tmpdir.join("cassette")
    persister.save_cassette(str(cassette_path), _cassette_dict(["a"]), WrappingSerializer)
    persister.load_cassette(str(cassette_path), WrappingSerializer)
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "b"]), WrappingSerializer)
    requests, _ = persister.load_cassette(str(cassette_path), WrappingSerializer)
    assert [request.uri.rsplit("/", 1)[1] for request in requests] == ["a", "b"]


def test_save_cassette_preserves_file_permissions(tmpdir):
    cassette_path = tmpdir.join("cassette")
    FilesystemPersister.save_cassette(str(cassette_path), _cassette_dict(["a"]), yamlserializer)
    cassette_path.chmod(0o640)
    FilesystemPersister.save_cassette(str(cassette_path), _cassette_dict(["a", "b"]), yamlserializer)
    assert stat.S_IMODE(cassette_path.stat().mode) == 0o640


def test_merging_persister_can_reload_and_save_after_a_conflict(tmpdir):
    persister = MergingFilesystemPersister
    cassette_path = tmpdir.join("cassette")
    persister.save_cassette(str(cassette_path), _cassette_dict(["a", "b"]), yamlserializer)

    # The other process rewrote position 1 after this one loaded the file.
    persister.load_cassette(str(cassette_path), yamlserializer)
    current = yamlserializer.deserialize(cassette_path.read())
    current["interactions"][1]["response"]["body"]["string"] = "from-the-other-process"
    cassette_path.write(yamlserializer.serialize(current))
    mine = _cassette_dict(["a", "b"])
    mine["responses"][1]["body"]["string"] = b"from-this-process"
    with pytest.raises(CassetteMergeConflictError):
        persister.save_cassette(str(cassette_path), mine, yamlserializer)

    # Reload: the file still holds the other process's version, and the
    # merged save can continue from it.
    requests, _ = persister.load_cassette(str(cassette_path), yamlserializer)
    assert [request.uri.rsplit("/", 1)[1] for request in requests] == ["a", "b"]
    assert yamlserializer.deserialize(cassette_path.read())["interactions"][1]["response"]["body"][
        "string"
    ] == "from-the-other-process"
    mine = _cassette_dict(["a", "b", "c"])
    # Keep the value reloaded from the file at position 1 instead of
    # reverting it, so the new save only adds the "c" interaction.
    mine["responses"][1]["body"]["string"] = b"from-the-other-process"
    persister.save_cassette(str(cassette_path), mine, yamlserializer)
    saved = yamlserializer.deserialize(cassette_path.read())
    assert [i["request"]["uri"].rsplit("/", 1)[1] for i in saved["interactions"]] == ["a", "b", "c"]
    assert saved["interactions"][1]["response"]["body"]["string"] == "from-the-other-process"


def test_merging_persister_merges_metadata(tmpdir):
    persister = MergingFilesystemPersister
    cassette_path = tmpdir.join("cassette")
    base = {**_cassette_dict(["a"]), "metadata": {"seed": 1}}
    persister.save_cassette(str(cassette_path), base, yamlserializer)
    persister.load_cassette(str(cassette_path), yamlserializer)

    # Another process adds an interaction and changes the metadata.
    current = {**_cassette_dict(["a", "theirs"]), "metadata": {"seed": 2}}
    FilesystemPersister.save_cassette(str(cassette_path), current, yamlserializer)
    mine = {**_cassette_dict(["a", "mine"]), "metadata": {"seed": 1}}
    persister.save_cassette(str(cassette_path), mine, yamlserializer)

    saved = yamlserializer.deserialize(cassette_path.read())
    assert [i["request"]["uri"].rsplit("/", 1)[1] for i in saved["interactions"]] == [
        "a",
        "theirs",
        "mine",
    ]
    assert saved["metadata"] == {"seed": 2}


def test_merging_persister_concurrent_saves_lose_nothing(tmpdir):
    persister = MergingFilesystemPersister
    cassette_path = str(tmpdir.join("cassette"))
    persister.save_cassette(cassette_path, _cassette_dict(["a"]), yamlserializer)

    barrier = threading.Barrier(2)

    def record(name):
        persister.load_cassette(cassette_path, yamlserializer)
        barrier.wait()  # both threads read the same generation
        persister.save_cassette(cassette_path, _cassette_dict(["a", name]), yamlserializer)

    threads = [threading.Thread(target=record, args=(name,)) for name in ("b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(_saved_names(cassette_path, yamlserializer)) == ["a", "b", "c"]
