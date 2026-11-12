import contextlib
import filecmp
import io
import json
import shutil
import sys
import warnings
from unittest import mock

import pytest
import yaml

import vcr.migration
from vcr.cassette import Cassette
from vcr.serialize import deserialize
from vcr.serializers import compat, yamlserializer

# Use the libYAML versions if possible
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


def test_try_migrate_with_json(tmpdir):
    cassette = tmpdir.join("cassette.json").strpath
    shutil.copy("tests/fixtures/migration/old_cassette.json", cassette)
    assert vcr.migration.try_migrate(cassette)
    with open("tests/fixtures/migration/new_cassette.json") as f:
        expected_json = json.load(f)
    with open(cassette) as f:
        actual_json = json.load(f)
    assert actual_json == expected_json


def test_try_migrate_with_yaml(tmpdir):
    cassette = tmpdir.join("cassette.yaml").strpath
    shutil.copy("tests/fixtures/migration/old_cassette.yaml", cassette)
    assert vcr.migration.try_migrate(cassette)
    with open("tests/fixtures/migration/new_cassette.yaml") as f:
        expected_yaml = yaml.load(f, Loader=Loader)
    with open(cassette) as f:
        actual_yaml = yaml.load(f, Loader=Loader)
    assert actual_yaml == expected_yaml


def test_try_migrate_with_invalid_or_new_cassettes(tmpdir):
    cassette = tmpdir.join("cassette").strpath
    files = [
        "tests/fixtures/migration/not_cassette.txt",
        "tests/fixtures/migration/new_cassette.yaml",
        "tests/fixtures/migration/new_cassette.json",
    ]
    for file_path in files:
        shutil.copy(file_path, cassette)
        assert not vcr.migration.try_migrate(cassette)
        assert filecmp.cmp(cassette, file_path)  # should not change file


LEGACY_HEADER_FIXTURES = [
    "raw_header_lines.yaml",  # 0.x writer: list of raw "Name: value" lines
    "flattened_header_value.yaml",  # hand-edited: same-name headers comma-joined
    "header_value_lists.yaml",  # hand-organized: already current shape
]


def _copy_fixture(tmpdir, name):
    cassette = tmpdir.join(name).strpath
    shutil.copy(f"tests/fixtures/migration/legacy_headers/{name}", cassette)
    return cassette


def _replay(cassette_path):
    """Load a cassette through the real replay path and return (headers, body).

    Headers come back as a mapping; compare it order-insensitively, since the
    YAML dumper sorts mapping keys (order preservation is covered separately
    by the raw header lines tests).
    """
    cassette = Cassette.load(path=cassette_path)
    assert len(cassette) == 1
    response = cassette.play_response(cassette.requests[0])
    return sorted(response["headers"].items()), response["body"]["string"]


# Version-1 cassettes replay before migration; the 0.x cassette can only be
# replayed after the migration entry point has converted it.
LOADABLE_FIXTURES = ["flattened_header_value.yaml", "header_value_lists.yaml"]


@pytest.mark.parametrize("name", LOADABLE_FIXTURES)
def test_legacy_response_headers_replay_unchanged_after_migration(tmpdir, name):
    cassette = _copy_fixture(tmpdir, name)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", compat.FlattenedHeadersWarning)
        before = _replay(cassette)
        vcr.migration.try_migrate(cassette)
        assert _replay(cassette) == before


def test_flattened_header_warns_on_load_as_well_as_migration(tmpdir):
    cassette = _copy_fixture(tmpdir, "flattened_header_value.yaml")
    with pytest.warns(compat.FlattenedHeadersWarning, match="set-cookie"):
        _replay(cassette)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", compat.FlattenedHeadersWarning)
        vcr.migration.try_migrate(cassette)
    # once normalized into a single-element list, loading is silent
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        _replay(cassette)
    assert not [w for w in recorded if w.category is compat.FlattenedHeadersWarning]


def test_zero_dot_x_cassette_replays_after_migration(tmpdir):
    cassette = _copy_fixture(tmpdir, "raw_header_lines.yaml")
    with pytest.raises(ValueError, match="problem loading the cassette"):
        _replay(cassette)
    assert vcr.migration.try_migrate(cassette)
    headers, body = _replay(cassette)
    header_dict = dict(headers)
    assert header_dict["set-cookie"] == ["session=abc123; Path=/", "theme=dark; Path=/"]
    assert header_dict["x-empty"] == [""]
    assert header_dict["cache-control"] == ["no-store, max-age=0"]
    assert body == b'{"ok": true}'


def test_raw_header_lines_keep_repeated_and_empty_headers(tmpdir):
    cassette = _copy_fixture(tmpdir, "raw_header_lines.yaml")
    assert vcr.migration.try_migrate(cassette)
    with open(cassette) as f:
        migrated = yaml.load(f, Loader=Loader)
    headers = migrated["interactions"][0]["response"]["headers"]
    assert headers["set-cookie"] == ["session=abc123; Path=/", "theme=dark; Path=/"]
    assert headers["x-empty"] == [""]
    assert "cache-control" in headers  # comma is payload here, not a flattened join
    assert migrated["interactions"][0]["request"]["headers"] == {"accept": ["*/*"]}


def test_flattened_header_value_is_kept_and_reported(tmpdir):
    cassette = _copy_fixture(tmpdir, "flattened_header_value.yaml")
    with pytest.warns(compat.FlattenedHeadersWarning, match="set-cookie"):
        assert vcr.migration.try_migrate(cassette)
    with open(cassette) as f:
        migrated = yaml.load(f, Loader=Loader)
    # The boundary is unrecoverable, so it stays one value rather than being
    # split on commas (which would invent headers that were never there).
    assert migrated["interactions"][0]["response"]["headers"]["set-cookie"] == [
        "session=abc123; Path=/, theme=dark; Path=/",
    ]


def test_flattened_input_is_the_one_that_already_lost_information(tmpdir):
    cassette = _copy_fixture(tmpdir, "flattened_header_value.yaml")
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        headers, _ = _replay(cassette)
    # The two original Set-Cookie headers were joined before VCR saw the file;
    # replay (and migration) can only ever observe this single value.
    assert [w for w in recorded if w.category is compat.FlattenedHeadersWarning]
    assert dict(headers)["set-cookie"] == [
        "session=abc123; Path=/, theme=dark; Path=/",
    ]
    # contrast: the same data in the list shape replays as two cookies
    intact = _copy_fixture(tmpdir, "header_value_lists.yaml")
    intact_headers, _ = _replay(intact)
    assert dict(intact_headers)["set-cookie"] == [
        "session=abc123; Path=/",
        "theme=dark; Path=/",
    ]


def test_header_value_lists_and_binary_body_need_no_change(tmpdir):
    cassette = _copy_fixture(tmpdir, "header_value_lists.yaml")
    with open(cassette, "rb") as f:
        original = f.read()
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert not vcr.migration.try_migrate(cassette)
    assert not [w for w in recorded if w.category is compat.FlattenedHeadersWarning]
    with open(cassette, "rb") as f:
        assert f.read() == original
    # binary body still replays as bytes
    assert _replay(cassette)[1] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("name", LEGACY_HEADER_FIXTURES)
def test_migrating_twice_is_idempotent(tmpdir, name):
    cassette = _copy_fixture(tmpdir, name)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        vcr.migration.try_migrate(cassette)
    with open(cassette) as f:
        first = yaml.load(f, Loader=Loader)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert not vcr.migration.try_migrate(cassette)
    assert not [w for w in recorded if w.category is compat.FlattenedHeadersWarning]
    with open(cassette) as f:
        assert yaml.load(f, Loader=Loader) == first


def test_failed_migration_does_not_overwrite_input(tmpdir):
    cassette = tmpdir.join("broken.yaml").strpath
    with open(cassette, "w") as f:
        f.write("version: 2\ninteractions: []\n")
    with open(cassette, "rb") as f:
        original = f.read()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        vcr.migration.try_migrate(cassette)
    with open(cassette, "rb") as f:
        assert f.read() == original


def test_batch_migration_keeps_going_and_leaves_bad_input_alone(tmpdir):
    bad = tmpdir.join("bad.yaml").strpath
    with open(bad, "w") as f:
        f.write("version: 2\ninteractions: []\n")
    good = _copy_fixture(tmpdir, "raw_header_lines.yaml")
    with open(bad, "rb") as f:
        bad_original = f.read()
    with open(good, "rb") as f:
        good_original = f.read()

    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["vcr.migration", tmpdir.strpath]):
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            vcr.migration.main()

    assert "refusing to overwrite" in stderr.getvalue()
    assert "[OK]" in stderr.getvalue()
    with open(bad, "rb") as f:
        assert f.read() == bad_original
    with open(good, "rb") as f:
        assert f.read() != good_original  # the good cassette was migrated


def test_unparseable_raw_header_line_fails_without_touching_input(tmpdir):
    cassette = tmpdir.join("bad_lines.yaml").strpath
    with open(cassette, "w") as f:
        f.write(
            yaml.dump(
                [
                    {
                        "request": {
                            "body": None,
                            "headers": [[["accept", "*/*"]]],
                            "host": "example.com",
                            "method": "GET",
                            "path": "/",
                            "port": 80,
                            "protocol": "http",
                        },
                        "response": {
                            "body": {"string": ""},
                            "headers": ["good: 1", "this line has no colon"],
                            "status": {"code": 200, "message": "OK"},
                        },
                    },
                ],
            ),
        )
    with open(cassette, "rb") as f:
        original = f.read()
    with pytest.raises(ValueError, match="unparseable response header line"):
        vcr.migration.try_migrate(cassette)
    with open(cassette, "rb") as f:
        assert f.read() == original


def test_deserialize_normalizes_legacy_response_headers():
    cassette_string = yaml.dump(
        {
            "version": 1,
            "interactions": [
                {
                    "request": {"body": None, "uri": "http://example.com/", "method": "GET", "headers": {}},
                    "response": {
                        "body": {"string": None},
                        "headers": [
                            "content-type: application/json\r\n",
                            "set-cookie: a=1\r\n",
                            "set-cookie: b=2\r\n",
                        ],
                        "status": {"code": 200, "message": "OK"},
                    },
                },
            ],
        },
    )
    requests, responses = deserialize(cassette_string, yamlserializer)
    assert responses[0]["headers"]["set-cookie"] == ["a=1", "b=2"]
    assert responses[0]["body"]["string"] is None
