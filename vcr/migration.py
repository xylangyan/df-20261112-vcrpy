"""
Migration script for old 'yaml' and 'json' cassettes

.. warning:: Backup your cassettes files before migration.

It merges and deletes the request obsolete keys (protocol, host, port, path)
into new 'uri' key.
Usage::

    python3 -m vcr.migration PATH

The PATH can be path to the directory with cassettes or cassette itself
"""

import json
import os
import shutil
import sys
import tempfile

import yaml

from . import request
from .serialize import serialize
from .serializers import jsonserializer, yamlserializer
from .serializers.compat import normalize_response_headers
from .serializers.yamlserializer import _CassetteLoader as Loader


def preprocess_yaml(cassette):
    # this is the hack that makes the whole thing work.  The old version used
    # to deserialize to Request objects automatically using pyYaml's !!python
    # tag system.  This made it difficult to deserialize old cassettes on new
    # versions.  So this just strips the tags before deserializing.

    STRINGS_TO_NUKE = [
        "!!python/object:vcr.request.Request",
        "!!python/object/apply:__builtin__.frozenset",
        "!!python/object/apply:builtins.frozenset",
    ]
    for s in STRINGS_TO_NUKE:
        cassette = cassette.replace(s, "")
    return cassette


PARTS = ["protocol", "host", "port", "path"]


def build_uri(**parts):
    port = parts["port"]
    scheme = parts["protocol"]
    default_port = {"https": 443, "http": 80}[scheme]
    parts["port"] = f":{port}" if port != default_port else ""
    return "{protocol}://{host}{port}{path}".format(**parts)


def _normalize_request_headers(headers):
    if headers is None:
        return {}
    if isinstance(headers, list):  # old shape: [["key", "value"], ...] or [("key", "value"), ...]
        if headers and isinstance(headers[0], (list, tuple)):
            return _list_of_tuples_to_dict(headers)
        return headers
    if isinstance(headers, dict):
        return {k: v if isinstance(v, (list, tuple)) else [v] for k, v in headers.items()}
    raise ValueError(f"unrecognized request headers shape: {type(headers).__name__}")


def _has_legacy_response_headers(headers):
    if isinstance(headers, (list, tuple)):
        return True
    if isinstance(headers, dict):
        return any(not isinstance(v, (list, tuple)) for v in headers.values())
    return False


def _migrate(data):
    interactions = []
    for index, item in enumerate(data):
        req = item["request"]
        res = item["response"]
        uri = {k: req.pop(k) for k in PARTS}
        req["uri"] = build_uri(**uri)
        # convert request headers to dict of lists, accepting the shapes the
        # old migration already had to deal with.
        req["headers"] = _normalize_request_headers(req.get("headers"))
        res["headers"] = normalize_response_headers(res.get("headers"), context=f"response {index}")
        interactions.append({"request": req, "response": res})
    return {
        "requests": [request.Request._from_dict(i["request"]) for i in interactions],
        "responses": [i["response"] for i in interactions],
    }


def _normalize_v1_responses(data):
    """Normalize legacy response header shapes in already version-1 cassettes.

    Returns a cassette dict ready for serialization, or ``None`` when nothing
    in the cassette needs normalization (in which case the file must not be
    touched).
    """
    interactions = data.get("interactions", [])
    responses = [interaction["response"] for interaction in interactions]
    if not any(_has_legacy_response_headers(response.get("headers")) for response in responses):
        return None
    for index, response in enumerate(responses):
        response["headers"] = normalize_response_headers(
            response.get("headers"),
            context=f"response {index}",
        )
    cassette_dict = {
        "requests": [request.Request._from_dict(interaction["request"]) for interaction in interactions],
        "responses": responses,
    }
    if data.get("metadata"):
        cassette_dict["metadata"] = data["metadata"]
    return cassette_dict


def migrate_json(in_fp, out_fp):
    data = json.load(in_fp)
    if isinstance(data, list):
        interactions = _migrate(data)
    elif _already_migrated(data):
        interactions = _normalize_v1_responses(data)
        if interactions is None:
            return False
    else:
        raise ValueError("unrecognized cassette format; refusing to overwrite it")
    out_fp.write(serialize(interactions, jsonserializer))
    return True


def _list_of_tuples_to_dict(fs):
    return dict(fs[0])


def _already_migrated(data):
    try:
        if data.get("version") == 1:
            return True
    except AttributeError:
        return False


def migrate_yml(in_fp, out_fp):
    data = yaml.load(preprocess_yaml(in_fp.read()), Loader=Loader)
    if isinstance(data, list):
        for i in range(len(data)):
            data[i]["request"]["headers"] = _list_of_tuples_to_dict(data[i]["request"]["headers"])
        interactions = _migrate(data)
    elif _already_migrated(data):
        interactions = _normalize_v1_responses(data)
        if interactions is None:
            return False
    else:
        raise ValueError("unrecognized cassette format; refusing to overwrite it")
    out_fp.write(serialize(interactions, yamlserializer))
    return True


def migrate(file_path, migration_fn):
    # because we assume that original files can be reverted
    # we will try to copy the content. (os.rename not needed)
    with tempfile.TemporaryFile(mode="w+") as out_fp:
        with open(file_path) as in_fp:
            if not migration_fn(in_fp, out_fp):
                return False
        with open(file_path, "w") as in_fp:
            out_fp.seek(0)
            shutil.copyfileobj(out_fp, in_fp)
        return True


def try_migrate(path):
    if path.endswith(".json"):
        return migrate(path, migrate_json)
    elif path.endswith((".yaml", ".yml")):
        return migrate(path, migrate_yml)
    return False


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Please provide path to cassettes directory or file. Usage: python3 -m vcr.migration PATH",
        )

    path = sys.argv[1]
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    files = [path]
    if os.path.isdir(path):
        files = (os.path.join(root, name) for (root, dirs, files) in os.walk(path) for name in files)
    for file_path in files:
        try:
            migrated = try_migrate(file_path)
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            # migrate() writes to a temp file and only copies it over the input
            # on success, so the failing cassette is left exactly as it was.
            sys.stderr.write(f"[FAIL] {file_path}: {exc}\n")
            continue
        status = "OK" if migrated else "SKIP"
        sys.stderr.write(f"[{status}] {file_path}\n")
    sys.stderr.write("Done.\n")


if __name__ == "__main__":
    main()
