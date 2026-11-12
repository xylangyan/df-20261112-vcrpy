"""Three-way merge of cassette files.

When several test processes record into the same cassette file, each one
reads the file, adds its own interactions and writes the whole file back.
The last process to save overwrites what the others just recorded. The
merging persister in ``vcr.persisters.filesystem`` avoids that by loading
the file again right before saving and merging it with what the process
was about to write.

The merge is a three-way merge over the cassette's serialized form:

- ``base``: the interactions as they were when the process loaded the
  cassette. The merging persister remembers them on load; a save without
  a matching load treats the cassette as new, with an empty base.
- ``theirs``: the interactions currently in the file, which may include
  what other processes recorded since ``base`` was read.
- ``mine``: the interactions the process wants to save.

The merge keeps the original order of the loaded interactions and
combines the additions of both sides, so one process's save no longer
drops what another process recorded in the meantime. Interactions that
both sides rewrote at the same position conflict, and are reported or
resolved according to the declared conflict policy.
"""

from enum import Enum


class ConflictPolicy(str, Enum):  # noqa: UP042 - StrEnum would change str() and format() of members
    """How a merging save handles interactions both sides rewrote.

    Can be declared by either using the enumerated value
    (``ConflictPolicy.ERROR``) or by simply using the defined string
    (``"error"``).

    `ERROR`: report the conflict by raising ``CassetteMergeConflictError``.
    `KEEP_THEIRS`: keep the interaction currently in the file.
    `KEEP_MINE`: keep the interaction this save was about to write.
    """

    ERROR = "error"
    KEEP_THEIRS = "keep_theirs"
    KEEP_MINE = "keep_mine"


def validate_conflict_policy(policy):
    """Coerce and validate a user-supplied conflict policy.

    Accepts either a ``ConflictPolicy`` or one of its string values and
    returns the corresponding ``ConflictPolicy``. Raises ``ValueError``
    with a helpful message for anything else.
    """
    try:
        return ConflictPolicy(policy)
    except ValueError:
        valid_policies = ", ".join(repr(policy.value) for policy in ConflictPolicy)
        raise ValueError(
            f"{policy!r} is not a valid conflict policy. Valid policies are: {valid_policies}.",
        ) from None


class CassetteMergeConflictError(Exception):
    """Raised when a merging save can't be merged automatically.

    The cassette file changed since it was loaded, and both sides rewrote
    the same interaction. ``conflicts`` lists them as
    ``(position, base, theirs, mine)`` tuples, where ``position`` is the
    interaction's index in the loaded cassette and the other three are
    the conflicting serialized interactions. The file is left unchanged.
    """

    def __init__(self, cassette_path, conflicts):
        self.cassette_path = cassette_path
        self.conflicts = conflicts
        positions = ", ".join(str(position) for position, _, _, _ in conflicts)
        super().__init__(
            f"Cassette {cassette_path!r} changed since it was loaded, and both sides rewrote "
            f"the interaction(s) at position(s) {positions}. Nothing was saved; reload the "
            "cassette and record again, or declare a conflict policy ('keep_theirs' or "
            "'keep_mine') to resolve such conflicts automatically.",
        )


def merge_cassette_dicts(base, theirs, mine, on_conflict=ConflictPolicy.ERROR, cassette_path=None):
    """Merge two saves of the same cassette into one cassette dict.

    ``base``, ``theirs`` and ``mine`` are cassette dicts in the
    serializer's form, with ``version``, ``interactions`` and optionally
    ``metadata`` keys. ``base`` is the cassette as it was loaded,
    ``theirs`` what's in the file now and ``mine`` what this save wants
    to write. Returns the merged cassette dict.

    Interactions both sides rewrote at the same position are resolved
    according to ``on_conflict``; with the default ``"error"`` policy a
    ``CassetteMergeConflictError`` is raised and nothing is merged.
    """
    on_conflict = validate_conflict_policy(on_conflict)
    base = base or {}
    theirs = theirs or {}
    mine = mine or {}
    base_interactions = base.get("interactions") or []
    their_interactions = theirs.get("interactions") or []
    my_interactions = mine.get("interactions") or []

    merged = []
    conflicts = []
    for position, base_interaction in enumerate(base_interactions):
        their_interaction = their_interactions[position] if position < len(their_interactions) else None
        my_interaction = my_interactions[position] if position < len(my_interactions) else None
        if their_interaction == my_interaction:
            # Unchanged, or both sides made the same change.
            if my_interaction is not None:
                merged.append(my_interaction)
        elif base_interaction == their_interaction:
            # Only this save changed it.
            if my_interaction is not None:
                merged.append(my_interaction)
        elif base_interaction == my_interaction:
            # Only the file changed since the cassette was loaded.
            if their_interaction is not None:
                merged.append(their_interaction)
        else:
            # Both sides rewrote this position differently.
            if on_conflict == ConflictPolicy.ERROR:
                conflicts.append((position, base_interaction, their_interaction, my_interaction))
                continue
            winner = their_interaction if on_conflict == ConflictPolicy.KEEP_THEIRS else my_interaction
            if winner is not None:
                merged.append(winner)
    if conflicts:
        raise CassetteMergeConflictError(cassette_path, conflicts)

    # Interactions appended after the loaded ones. Entries both sides
    # recorded identically are kept once.
    merged.extend(their_interactions[len(base_interactions) :])
    for interaction in my_interactions[len(base_interactions) :]:
        if interaction not in merged:
            merged.append(interaction)

    merged_dict = {"version": mine.get("version", theirs.get("version", base.get("version")))}
    merged_dict["interactions"] = merged
    metadata = _merge_metadata(base.get("metadata"), theirs.get("metadata"), mine.get("metadata"))
    if metadata is not None:
        merged_dict["metadata"] = metadata
    return merged_dict


def _merge_metadata(base, theirs, mine):
    """Merge the cassette's metadata, treating it as a single value."""
    if theirs == mine:
        return mine
    if base == theirs:
        return mine
    if base == mine:
        return theirs
    # Both sides changed it: the save being made wins, like the
    # "keep_mine" policy, so a save never silently drops its own values.
    return mine
