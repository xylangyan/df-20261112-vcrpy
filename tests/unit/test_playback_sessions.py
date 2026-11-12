"""Independent playback sessions over a shared cassette.

The cassette used here models a paginating API whose server sometimes
returns the same next-page URL twice in a row, so two of the three recorded
interactions share one URI. Two sessions replaying it concurrently must not
consume each other's responses: each keeps its own cursor over the shared,
read-only interactions.
"""

import json

import pytest

from vcr.cassette import Cassette
from vcr.errors import UnhandledHTTPRequestError
from vcr.request import Request

CASSETTE_PATH = "tests/fixtures/playback_sessions/three_pages.yaml"

FIRST_PAGE = "http://example.com/items"
# The buggy API hands out this same "next page" URL twice in a row.
REPEATED_PAGE = "http://example.com/items?cursor=abc"


def get(uri):
    return Request("GET", uri, None, {})


def page_of(response):
    return json.loads(response["body"]["string"].decode("utf-8"))["page"]


def load_cassette(**kwargs):
    return Cassette.load(path=CASSETTE_PATH, **kwargs)


def test_sessions_share_interactions_but_not_progress():
    cassette = load_cassette()
    assert len(cassette) == 3

    with cassette.playback_session() as first, cassette.playback_session() as second:
        # The two sessions take turns asking for pages.
        assert page_of(first.play_response(get(FIRST_PAGE))) == 1
        assert page_of(second.play_response(get(FIRST_PAGE))) == 1

        # The repeated next-page URL is where a shared cursor would hand the
        # second session page 3 instead of page 2.
        assert page_of(first.play_response(get(REPEATED_PAGE))) == 2
        assert page_of(second.play_response(get(REPEATED_PAGE))) == 2

        assert page_of(first.play_response(get(REPEATED_PAGE))) == 3
        assert page_of(second.play_response(get(REPEATED_PAGE))) == 3

        assert first.all_played and second.all_played
        assert first.remaining == 0 and second.remaining == 0

    # Sessions only read the cassette: its own cursor never moved.
    assert cassette.play_count == 0
    assert not cassette.all_played


def test_early_exit_releases_only_its_own_session():
    cassette = load_cassette()
    first = cassette.playback_session()
    second = cassette.playback_session()

    assert page_of(first.play_response(get(FIRST_PAGE))) == 1
    assert page_of(second.play_response(get(FIRST_PAGE))) == 1
    assert page_of(first.play_response(get(REPEATED_PAGE))) == 2

    class EarlyExit(Exception):
        pass

    # The first session bails out abnormally after page 2.
    with pytest.raises(EarlyExit), first:
        raise EarlyExit

    assert first.closed
    # The early exit releases the session but keeps its final progress.
    assert first.play_count == 2
    assert first.remaining == 1
    with pytest.raises(ValueError, match="closed"):
        first.play_response(get(FIRST_PAGE))

    # The other session's cursor is untouched: it still gets pages 2 and 3.
    assert page_of(second.play_response(get(REPEATED_PAGE))) == 2
    assert page_of(second.play_response(get(REPEATED_PAGE))) == 3
    assert second.all_played
    assert second.remaining == 0

    # Closing the second session afterwards is fine and still touches nothing else.
    second.close()
    assert second.closed
    assert cassette.play_count == 0


def test_session_exhaustion_message_matches_cassette():
    cassette = load_cassette()
    session = cassette.playback_session()
    request = get(FIRST_PAGE)
    session.play_response(request)
    with pytest.raises(UnhandledHTTPRequestError) as session_error:
        session.play_response(request)

    fresh = load_cassette()
    fresh.play_response(request)
    with pytest.raises(UnhandledHTTPRequestError) as cassette_error:
        fresh.play_response(request)

    assert str(session_error.value) == str(cassette_error.value)


def test_session_respects_allow_playback_repeats():
    cassette = load_cassette(allow_playback_repeats=True)
    with cassette.playback_session() as session:
        assert session.allow_playback_repeats
        for expected_page in (1, 1, 1):
            assert page_of(session.play_response(get(FIRST_PAGE))) == expected_page
        assert session.play_count == 3
        # Repeats of one interaction don't mark the others as played.
        assert not session.all_played
        assert session.remaining == 2


def test_session_rewind_resets_only_its_own_cursor():
    cassette = load_cassette()
    first = cassette.playback_session()
    second = cassette.playback_session()

    assert page_of(first.play_response(get(FIRST_PAGE))) == 1
    assert page_of(second.play_response(get(FIRST_PAGE))) == 1

    first.rewind()
    assert not first.all_played
    assert first.remaining == 3
    # The other session is not rewound along with it.
    assert second.remaining == 2

    assert page_of(first.play_response(get(FIRST_PAGE))) == 1
    with pytest.raises(UnhandledHTTPRequestError):
        second.play_response(get(FIRST_PAGE))


def test_session_membership_and_can_play_response_for():
    cassette = load_cassette()
    session = cassette.playback_session()

    assert get(FIRST_PAGE) in session
    assert session.can_play_response_for(get(FIRST_PAGE))

    session.play_response(get(FIRST_PAGE))
    assert get(FIRST_PAGE) not in session
    assert not session.can_play_response_for(get(FIRST_PAGE))
    # The repeated URL still has two unplayed interactions.
    assert get(REPEATED_PAGE) in session

    # The cassette itself is unaffected by the session's consumption.
    assert get(FIRST_PAGE) in cassette


def test_closed_session_cannot_be_reentered():
    cassette = load_cassette()
    session = cassette.playback_session()
    with session:
        pass
    assert session.closed
    with pytest.raises(ValueError, match="closed"):
        with session:
            pass
    # Closing twice is harmless.
    session.close()
