"""Local contract tests for the httpx (and httpx2) playback entry points.

A VCR stub returns a fresh ``httpx.ByteStream`` for every played interaction, so
nothing about how far the caller drains that stream reaches the cassette. These
tests pin that contract down for both the sync (``handle_request``) and async
(``handle_async_request``) paths, and make three operation sequences explicit:

* read only the first chunk, then let the ``stream`` context close the response;
* close the response without reading any body bytes at all;
* raise from inside the body iteration, while still inside ``stream``.

The cassette is built up front with a local ``MockTransport``: no network, no
httpbin. Each scenario also asserts that the client left neither an open
connection nor (on the async path) a task or coroutine behind, so a regression
that skips ``aclose``/``close`` shows up here instead of as a hanging loop.

If sync and async ever diverge, the minimal reproducer is a scenario id from
the ``SYNC_SEQUENCES`` / ``ASYNC_SEQUENCES`` tables below run through its
matching test; the two tables keep the same ids and order on purpose so the
diverging operation sequence is named in the test id.
"""

import asyncio
import warnings

import pytest

import vcr
from vcr.errors import CannotOverwriteExistingCassetteException


@pytest.fixture(params=["httpx", "httpx2"])
def httpx_module(request):
    return pytest.importorskip(request.param)


URLS = ("http://example.invalid/one", "http://example.invalid/two")
BODY_CHUNKS = (b"chunk-1", b"chunk-2")
BODY = b"".join(BODY_CHUNKS)


class StreamCounter:
    """Records how often a response stream was read and closed.

    The recording phase uses it to prove that VCR drains a recorded stream to
    the end even when the caller stops after the first chunk.
    """

    def __init__(self):
        self.reads = 0
        self.closes = 0

    def read(self):
        self.reads += 1

    def close(self):
        self.closes += 1


class CountingSyncStream:
    def __init__(self, counter, chunks):
        self._counter = counter
        self._chunks = chunks

    def __iter__(self):
        for chunk in self._chunks:
            self._counter.read()
            yield chunk

    def close(self):
        self._counter.close()


class CountingAsyncStream:
    def __init__(self, counter, chunks):
        self._counter = counter
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            self._counter.read()
            yield chunk

    async def aclose(self):
        self._counter.close()


def _record_cassette(httpx, path, urls):
    """Record every URL once through a local MockTransport, fully offline."""
    counter = StreamCounter()

    def handler(request):
        stream = CountingSyncStream(counter, BODY_CHUNKS)
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with vcr.use_cassette(path) as cassette:
            for url in urls:
                assert client.get(url).content == BODY

    assert len(cassette) == len(urls)
    return counter


@pytest.fixture
def cassette_path(tmp_path, httpx_module):
    path = tmp_path / "playback_contract.yaml"
    _record_cassette(httpx_module, str(path), URLS)
    return str(path)


def _pool_connections(client):
    """Held httpcore connections, the state a missed close would leave behind."""
    return list(client._transport._pool.connections)


def _assert_no_leaked_connections(client):
    held = _pool_connections(client)
    assert client.is_closed, "the client was left open"
    assert held == [], f"connections still held after playback: {held!r}"


async def _assert_no_pending_tasks():
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if not task.done() and task is not current]
    assert pending == [], f"tasks still pending after playback: {pending!r}"


# --- operation sequences ---------------------------------------------------


def _sync_read_first_chunk(client, url):
    with client.stream("GET", url) as response:
        first = next(response.iter_raw())
        assert first == BODY
        assert response.is_stream_consumed
        assert not response.is_closed
    assert response.is_closed


def _sync_close_without_reading(client, url):
    with client.stream("GET", url) as response:
        assert not response.is_stream_consumed
        assert not response.is_closed
    assert response.is_closed
    assert not response.is_stream_consumed


def _sync_raise_while_consuming(client, url):
    failure = RuntimeError("collector rejected this record")
    with pytest.raises(RuntimeError, match="collector rejected"):
        with client.stream("GET", url) as response:
            for _ in response.iter_raw():
                raise failure
            pytest.fail("the collector error must propagate")
    assert response.is_closed
    assert response.is_stream_consumed


async def _async_read_first_chunk(client, url):
    async with client.stream("GET", url) as response:
        async for chunk in response.aiter_raw():
            assert chunk == BODY
            break  # collector closes after the first qualifying record
        assert response.is_stream_consumed
        assert not response.is_closed
    assert response.is_closed


async def _async_close_without_reading(client, url):
    async with client.stream("GET", url) as response:
        assert not response.is_stream_consumed
        assert not response.is_closed
    assert response.is_closed
    assert not response.is_stream_consumed


async def _async_raise_while_consuming(client, url):
    failure = RuntimeError("collector rejected this record")
    with pytest.raises(RuntimeError, match="collector rejected"):
        async with client.stream("GET", url) as response:
            async for _ in response.aiter_raw():
                raise failure
            pytest.fail("the collector error must propagate")
    assert response.is_closed
    assert response.is_stream_consumed


# Each entry pairs a scenario id with its operation sequence. The two tables
# intentionally keep the same order and ids, so a divergence between the
# transport entry points shows up as a mismatch between identically named cases.
SYNC_SEQUENCES = [
    ("reads_first_chunk_then_closes", _sync_read_first_chunk),
    ("closes_without_reading", _sync_close_without_reading),
    ("raises_while_consuming", _sync_raise_while_consuming),
]

ASYNC_SEQUENCES = [
    ("reads_first_chunk_then_closes", _async_read_first_chunk),
    ("closes_without_reading", _async_close_without_reading),
    ("raises_while_consuming", _async_raise_while_consuming),
]


# --- contract: recording drains the real stream ----------------------------


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_recording_drains_and_closes_the_stream_even_if_the_caller_stops(
    tmp_path,
    httpx_module,
    asynchronous,
):
    """A collector that closes after the first record cannot stop VCR's read.

    VCR consumes the whole response stream and closes it before the caller
    sees the first byte, so "first record qualifies -> close early" never
    truncates what lands in the cassette.
    """
    httpx = httpx_module
    counter = StreamCounter()

    def handler(request):
        return httpx.Response(200, stream=CountingSyncStream(counter, BODY_CHUNKS))

    def consume_first_chunk(client):
        with client.stream("GET", URLS[0]) as response:
            assert next(response.iter_raw()) == BODY  # VCR already merged the chunks

    async def async_consume_first_chunk(client):
        async with client.stream("GET", URLS[0]) as response:
            async for chunk in response.aiter_raw():
                assert chunk == BODY
                break

    path = str(tmp_path / "recorded.yaml")
    if asynchronous:

        async def record():
            async def async_handler(request):
                return httpx.Response(200, stream=CountingAsyncStream(counter, BODY_CHUNKS))

            async with httpx.AsyncClient(transport=httpx.MockTransport(async_handler)) as client:
                with vcr.use_cassette(path) as cassette:
                    await async_consume_first_chunk(client)
            return cassette

        cassette = asyncio.run(record())
    else:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with vcr.use_cassette(path) as cassette:
                consume_first_chunk(client)

    assert counter.reads == len(BODY_CHUNKS)
    assert counter.closes == 1
    assert len(cassette) == 1


# --- contract: sync playback ------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "sequence"),
    SYNC_SEQUENCES,
    ids=[scenario for scenario, _ in SYNC_SEQUENCES],
)
def test_sync_playback_marks_interaction_used_before_any_body_is_read(
    cassette_path,
    httpx_module,
    scenario,
    sequence,
):
    """The interaction counts as used the moment the response is handed out.

    ``play_count`` goes to 1 for every sequence, including closing without
    reading, because the stub increments it before returning the response.
    """
    httpx = httpx_module
    with vcr.use_cassette(cassette_path) as cassette:
        with httpx.Client() as client:
            sequence(client, URLS[0])
        _assert_no_leaked_connections(client)
        assert cassette.play_count == 1
        assert not cassette.all_played  # the second URL is still unused


@pytest.mark.parametrize(
    ("scenario", "sequence"),
    SYNC_SEQUENCES,
    ids=[scenario for scenario, _ in SYNC_SEQUENCES],
)
def test_sync_playback_lets_the_next_interaction_through_after_an_error(
    cassette_path,
    httpx_module,
    scenario,
    sequence,
):
    """A failed or partial consumption never blocks later interactions."""
    httpx = httpx_module
    with vcr.use_cassette(cassette_path) as cassette:
        with httpx.Client() as client:
            sequence(client, URLS[0])
            assert client.get(URLS[1]).content == BODY
        _assert_no_leaked_connections(client)
        assert cassette.play_count == 2
        assert cassette.all_played


def test_sync_playback_rejects_repeating_a_used_interaction(cassette_path, httpx_module):
    """Without allow_playback_repeats, an errored interaction stays spent."""
    httpx = httpx_module
    with vcr.use_cassette(cassette_path) as cassette:
        with httpx.Client() as client:
            _sync_raise_while_consuming(client, URLS[0])
            with pytest.raises(CannotOverwriteExistingCassetteException) as caught:
                client.get(URLS[0])
        _assert_no_leaked_connections(client)
        assert caught.value.reason == "already_played"
        assert cassette.play_count == 1


def test_sync_playback_allow_playback_repeats_recovers_after_an_error(
    cassette_path,
    httpx_module,
):
    httpx = httpx_module
    with vcr.use_cassette(cassette_path, allow_playback_repeats=True) as cassette:
        with httpx.Client() as client:
            _sync_raise_while_consuming(client, URLS[0])
            assert client.get(URLS[0]).content == BODY
        _assert_no_leaked_connections(client)
        assert cassette.play_count == 2


# --- contract: async playback -----------------------------------------------


def _run_async_with_leak_guards(coroutine_factory):
    """Drive one async test body and surface leaked tasks/coroutines.

    A missing ``aclose`` typically leaves an httpcore task pending until the
    loop is torn down. The task table is checked while the loop is still
    running, so a hang becomes an assertion instead of a slow test. Wrapping
    ``asyncio.run`` in a warning recorder catches coroutines that were created
    but never awaited; ``asyncio.run`` itself finalises suspended async
    generators during shutdown, so it does not mask a missing ``aclose`` on the
    response stream (that is asserted on the response object instead).
    """
    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        asyncio.run(coroutine_factory())

    never_awaited = [str(w.message) for w in caught_warnings if "was never awaited" in str(w.message)]
    assert never_awaited == [], f"coroutines were never awaited: {never_awaited!r}"


@pytest.mark.parametrize(
    ("scenario", "sequence"),
    ASYNC_SEQUENCES,
    ids=[scenario for scenario, _ in ASYNC_SEQUENCES],
)
def test_async_playback_marks_interaction_used_before_any_body_is_read(
    cassette_path,
    httpx_module,
    scenario,
    sequence,
):
    """Async counterpart of the sync contract, including the leak guards."""
    httpx = httpx_module

    async def run():
        with vcr.use_cassette(cassette_path) as cassette:
            async with httpx.AsyncClient() as client:
                await sequence(client, URLS[0])
            _assert_no_leaked_connections(client)
            await _assert_no_pending_tasks()
            assert cassette.play_count == 1
            assert not cassette.all_played

    _run_async_with_leak_guards(run)


@pytest.mark.parametrize(
    ("scenario", "sequence"),
    ASYNC_SEQUENCES,
    ids=[scenario for scenario, _ in ASYNC_SEQUENCES],
)
def test_async_playback_lets_the_next_interaction_through_after_an_error(
    cassette_path,
    httpx_module,
    scenario,
    sequence,
):
    httpx = httpx_module

    async def run():
        with vcr.use_cassette(cassette_path) as cassette:
            async with httpx.AsyncClient() as client:
                await sequence(client, URLS[0])
                response = await client.get(URLS[1])
                assert response.content == BODY
            _assert_no_leaked_connections(client)
            await _assert_no_pending_tasks()
            assert cassette.play_count == 2
            assert cassette.all_played

    _run_async_with_leak_guards(run)


def test_async_playback_rejects_repeating_a_used_interaction(cassette_path, httpx_module):
    httpx = httpx_module

    async def run():
        with vcr.use_cassette(cassette_path) as cassette:
            async with httpx.AsyncClient() as client:
                await _async_raise_while_consuming(client, URLS[0])
                with pytest.raises(CannotOverwriteExistingCassetteException) as caught:
                    await client.get(URLS[0])
            _assert_no_leaked_connections(client)
            await _assert_no_pending_tasks()
            assert caught.value.reason == "already_played"
            assert cassette.play_count == 1

    _run_async_with_leak_guards(run)


def test_async_playback_allow_playback_repeats_recovers_after_an_error(
    cassette_path,
    httpx_module,
):
    httpx = httpx_module

    async def run():
        with vcr.use_cassette(cassette_path, allow_playback_repeats=True) as cassette:
            async with httpx.AsyncClient() as client:
                await _async_raise_while_consuming(client, URLS[0])
                response = await client.get(URLS[0])
                assert response.content == BODY
            _assert_no_leaked_connections(client)
            await _assert_no_pending_tasks()
            assert cassette.play_count == 2

    _run_async_with_leak_guards(run)
