import pytest

from vcr.cassette_merge import (
    CassetteMergeConflictError,
    ConflictPolicy,
    merge_cassette_dicts,
    validate_conflict_policy,
)


def interaction(name, body=None):
    return {
        "request": {"method": "GET", "uri": f"http://example.com/{name}", "body": None, "headers": {}},
        "response": {
            "status": {"code": 200, "message": "OK"},
            "headers": {},
            "body": {"string": body or name},
        },
    }


def cassette(*names):
    return {"version": 1, "interactions": [interaction(name) for name in names]}


def changed(cassette_dict, position, body):
    interactions = list(cassette_dict["interactions"])
    interaction = {**interactions[position]}
    interaction["response"] = {**interaction["response"], "body": {"string": body}}
    interactions[position] = interaction
    return {**cassette_dict, "interactions": interactions}


def names(cassette_dict):
    return [interaction["request"]["uri"].rsplit("/", 1)[1] for interaction in cassette_dict["interactions"]]


def test_merge_appends_interactions_from_both_sides_in_recorded_order():
    base = cassette("a")
    merged = merge_cassette_dicts(base, cassette("a", "theirs"), cassette("a", "mine"))
    assert names(merged) == ["a", "theirs", "mine"]


def test_merge_keeps_each_sides_additions_when_none_overlap():
    base = cassette("a", "b")
    merged = merge_cassette_dicts(base, cassette("a", "b", "theirs"), cassette("a", "b", "mine"))
    assert names(merged) == ["a", "b", "theirs", "mine"]


def test_merge_keeps_the_original_sequence_when_mine_changes_nothing():
    base = cassette("a", "b", "c")
    merged = merge_cassette_dicts(base, cassette("a", "b", "c", "theirs"), base)
    assert names(merged) == ["a", "b", "c", "theirs"]


def test_merge_takes_the_files_version_of_interactions_mine_did_not_change():
    base = cassette("a", "b")
    theirs = changed(cassette("a", "b"), 0, "theirs")
    merged = merge_cassette_dicts(base, theirs, cassette("a", "b", "mine"))
    assert names(merged) == ["a", "b", "mine"]
    assert merged["interactions"][0]["response"]["body"]["string"] == "theirs"


def test_merge_takes_mine_version_for_interactions_the_file_did_not_change():
    base = cassette("a", "b")
    mine = changed(cassette("a", "b"), 1, "mine")
    merged = merge_cassette_dicts(base, cassette("a", "b"), mine)
    assert names(merged) == ["a", "b"]
    assert merged["interactions"][1]["response"]["body"]["string"] == "mine"


def test_merge_keeps_an_identical_addition_once():
    base = cassette("a")
    merged = merge_cassette_dicts(base, cassette("a", "same"), cassette("a", "same"))
    assert names(merged) == ["a", "same"]


def test_merge_drops_an_interaction_deleted_by_both_sides():
    base = cassette("a", "b")
    merged = merge_cassette_dicts(base, cassette("a"), cassette("a"))
    assert names(merged) == ["a"]


def test_merge_reports_conflicts_at_the_changed_positions():
    base = cassette("a", "b")
    theirs = changed(cassette("a", "b"), 1, "theirs")
    mine = changed(cassette("a", "b"), 1, "mine")
    with pytest.raises(CassetteMergeConflictError) as excinfo:
        merge_cassette_dicts(base, theirs, mine, cassette_path="cass.yaml")
    error = excinfo.value
    assert error.cassette_path == "cass.yaml"
    assert [position for position, _, _, _ in error.conflicts] == [1]
    assert "position(s) 1" in str(error)


def test_keep_theirs_and_keep_mine_policies_resolve_conflicts():
    base = cassette("a", "b")
    theirs = changed(cassette("a", "b"), 1, "theirs")
    mine = changed(cassette("a", "b"), 1, "mine")
    kept_theirs = merge_cassette_dicts(base, theirs, mine, ConflictPolicy.KEEP_THEIRS)
    kept_mine = merge_cassette_dicts(base, theirs, mine, "keep_mine")
    assert kept_theirs["interactions"][1]["response"]["body"]["string"] == "theirs"
    assert kept_mine["interactions"][1]["response"]["body"]["string"] == "mine"


def test_conflict_policy_accepts_enums_and_strings():
    assert validate_conflict_policy("keep_mine") == ConflictPolicy.KEEP_MINE
    assert validate_conflict_policy(ConflictPolicy.ERROR) == ConflictPolicy.ERROR
    with pytest.raises(ValueError, match="not a valid conflict policy"):
        validate_conflict_policy("overwrite")


def test_merge_keeps_metadata_from_the_save_that_changed_it():
    # Only one side changed the metadata: use that side's value.
    base = {"version": 1, "interactions": [], "metadata": {"seed": 1}}
    theirs = {"version": 1, "interactions": [], "metadata": {"seed": 2}}
    merged = merge_cassette_dicts(base, theirs, base)
    assert merged["metadata"] == {"seed": 2}

    base = cassette("a")
    mine = {**base, "metadata": {"seed": 1}}
    theirs = {**base, "metadata": {"seed": 2}}
    assert merge_cassette_dicts(base, theirs, mine)["metadata"] == {"seed": 1}
