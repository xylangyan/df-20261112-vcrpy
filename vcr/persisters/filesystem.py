# .. _persister_example:

import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

from ..cassette_merge import ConflictPolicy, merge_cassette_dicts, validate_conflict_policy
from ..serialize import deserialize, serialize


class CassetteNotFoundError(FileNotFoundError):
    pass


class CassetteDecodeError(ValueError):
    pass


def _read_cassette(cassette_path):
    """Read a cassette file's text, or return None if it isn't there."""
    cassette_path = Path(cassette_path)
    if not cassette_path.is_file():
        return None
    try:
        data = cassette_path.read_text()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as err:
        raise CassetteDecodeError("Can't read Cassette, Encoding is broken") from err
    if not data.strip():
        # An empty file is treated like a missing cassette and recorded again, as in Ruby VCR.
        return None
    return data


@contextmanager
def _cassette_lock(cassette_path):
    """Hold an exclusive lock on a lock file next to the cassette.

    Serializes merging saves, which read the cassette and replace it, so
    two processes can't both read the same generation and each replace
    the file with its own merged copy. The merge itself stays inside
    this lock, so each save sees the previous save's result.
    """
    lock_path = cassette_path.with_name(f".{cassette_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _lock_file(lock_file):
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - Windows
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(lock_file):
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _atomic_write(cassette_path, data):
    """Write data to a temporary file, then replace the cassette with it.

    The cassette itself is only touched by ``os.replace``, which is
    atomic on the same filesystem, so a reader never sees a half-written
    file, even if the process exits while saving.
    """
    cassette_path = Path(cassette_path)
    try:
        mode = cassette_path.stat().st_mode & 0o777
    except FileNotFoundError:
        # New cassette: the usual permissions of a file created with open().
        mode = 0o666 & ~0o022
    fd, temporary_path = tempfile.mkstemp(
        dir=str(cassette_path.parent),
        prefix=f".{cassette_path.name}.",
        suffix=".tmp",
    )
    try:
        os.chmod(temporary_path, mode)
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, cassette_path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


class FilesystemPersister:
    """The default cassette persister, which stores cassettes in files.

    Use ``MergingFilesystemPersister`` (or a subclass with
    ``safe_append = True``) when more than one process records into the
    same cassette. On load, the persister remembers the cassette's
    interactions; on save, it reads the file again and three-way merges
    the file, the remembered interactions and the ones being saved, so
    interactions other processes appended in the meantime are kept.
    ``on_conflict`` declares what to do when both sides rewrote the same
    interaction: by default the save is refused with a
    ``CassetteMergeConflictError``, ``"keep_theirs"`` keeps the file's
    version and ``"keep_mine"`` keeps the version being saved.

    Both the default and merging persisters write through a temporary
    file and atomically replace the cassette, so a reader never sees a
    half-written file.
    """

    safe_append = False
    on_conflict = ConflictPolicy.ERROR

    # Cassette text keyed by path and serializer, from the most recent
    # load, used as the merge base. The merge itself happens on the
    # serializer's plain-dict form rather than on Request objects, so the
    # base, the current file and the cassette being saved compare
    # identically even after serialize/deserialize round-trips.
    _loaded_cassettes = {}
    _lock = threading.Lock()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Each persister class tracks the cassettes it loaded on its own.
        cls._loaded_cassettes = {}
        cls._lock = threading.Lock()

    @classmethod
    def _base_key(cls, cassette_path, serializer):
        # Serializers may be unhashable user objects, so key by identity.
        return (Path(cassette_path), id(serializer))

    @classmethod
    def _remember_base(cls, cassette_path, serializer, data):
        with cls._lock:
            cls._loaded_cassettes[cls._base_key(cassette_path, serializer)] = (serializer, data)

    @classmethod
    def _known_base(cls, cassette_path, serializer):
        """The cassette as this persister last loaded it, if it did."""
        with cls._lock:
            base = cls._loaded_cassettes.get(cls._base_key(cassette_path, serializer))
        # Keeping the serializer reference makes a recycled id identifiable.
        if base is None or base[0] is not serializer:
            return None
        return base[1]

    @classmethod
    def _discard_base(cls, cassette_path, serializer):
        with cls._lock:
            cls._loaded_cassettes.pop(cls._base_key(cassette_path, serializer), None)

    @classmethod
    def load_cassette(cls, cassette_path, serializer):
        data = _read_cassette(cassette_path)
        if data is None:
            raise CassetteNotFoundError()
        cassette_dict = deserialize(data, serializer)
        if cls.safe_append:
            cls._remember_base(cassette_path, serializer, data)
        return cassette_dict

    @classmethod
    def save_cassette(cls, cassette_path, cassette_dict, serializer):
        """Save a cassette, replacing the file atomically."""
        cassette_path = Path(cassette_path)
        cassette_folder = cassette_path.parent
        if not cassette_folder.exists():
            cassette_folder.mkdir(parents=True)
        if cls.safe_append:
            # The merge reads the file and replaces it, so it must hold
            # the lock to see the previous save's result.
            with _cassette_lock(cassette_path):
                data = cls._merged_serialization(cassette_path, cassette_dict, serializer)
                _atomic_write(cassette_path, data)
            cls._discard_base(cassette_path, serializer)
            return
        data = serialize(cassette_dict, serializer)
        _atomic_write(cassette_path, data)

    @classmethod
    def _merged_serialization(cls, cassette_path, cassette_dict, serializer):
        """Serialize a save, merged with what the file holds right now."""
        # Normalize to the serializer's plain-dict form, so the merge
        # compares like with like.
        mine = serializer.deserialize(serialize(cassette_dict, serializer))
        current_data = _read_cassette(cassette_path)
        if current_data is None:
            return serializer.serialize(mine)
        current_dict = serializer.deserialize(current_data)
        base_data = cls._known_base(cassette_path, serializer)
        base_dict = serializer.deserialize(base_data) if base_data is not None else {}
        merged = merge_cassette_dicts(
            base_dict,
            current_dict,
            mine,
            cls.on_conflict,
            cassette_path=cassette_path,
        )
        return serializer.serialize(merged)

    @classmethod
    def configure(cls, safe_append=False, on_conflict=ConflictPolicy.ERROR):
        """Build a configured persister class.

        Returns a ``FilesystemPersister`` subclass usable with
        ``VCR.register_persister`` or the ``persister`` option. With
        ``safe_append=False`` (the default) it behaves exactly like
        ``FilesystemPersister``; with ``safe_append=True`` concurrent
        recordings are merged, and ``on_conflict`` (``"error"``,
        ``"keep_theirs"`` or ``"keep_mine"``) handles interactions both
        sides rewrote.
        """
        on_conflict = validate_conflict_policy(on_conflict)
        return type(
            "ConfiguredFilesystemPersister",
            (cls,),
            {"safe_append": safe_append, "on_conflict": on_conflict},
        )


class MergingFilesystemPersister(FilesystemPersister):
    """A filesystem persister that merges concurrent recordings by default."""

    safe_append = True
    on_conflict = ConflictPolicy.ERROR
