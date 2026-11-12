import warnings

from vcr.stubs.compat import get_httpmessage


class FlattenedHeadersWarning(UserWarning):
    """A cassette stores several same-name response headers as one flattened string.

    The original per-header boundaries cannot be recovered, so the value is
    kept as a single header entry and replay may differ from what was
    originally recorded.
    """


def _parse_header_lines(lines, context):
    # http.client.parse_headers silently ignores lines without a colon, which
    # would lose recorded data. Fail loudly instead; the migration caller
    # writes to a temp file first, so a failure leaves the input untouched.
    # Lines starting with whitespace are valid folded continuations.
    for line in lines:
        if not line.strip() or (line[:1] not in (b" ", b"\t") and b":" not in line):
            raise ValueError(f"{context}: unparseable response header line: {line!r}")
    headers = {}
    for key, value in get_httpmessage(b"".join(lines)).items():
        headers.setdefault(key, []).append(value)
    return headers


def normalize_response_headers(headers, context="response"):
    """Convert recognizable legacy response header shapes to a dict of lists.

    Recognized shapes:

    - a list of raw header lines (``"Key: value"`` strings or bytes), as
      recorded by VCR.py 0.x: parsed, preserving order and repeated names;
    - a dict of name -> list of values (the current format): kept as is;
    - a dict of name -> a single value: wrapped in one-element lists. When
      the value looks like several headers flattened into one comma-joined
      string, a :class:`FlattenedHeadersWarning` is emitted for the record,
      because the original boundaries cannot be recovered.

    ``None`` and empty values are preserved. Anything else raises a
    ``ValueError`` instead of being silently mangled.
    """
    if headers is None:
        return {}
    if isinstance(headers, (list, tuple)):
        lines = []
        for line in headers:
            if isinstance(line, str):
                line = line.encode("utf-8")
            elif not isinstance(line, (bytes, bytearray)):
                raise ValueError(f"{context}: unparseable response header line: {line!r}")
            lines.append(line)
        return _parse_header_lines(lines, context)
    if isinstance(headers, dict):
        normalized = {}
        for key, values in headers.items():
            if isinstance(values, (list, tuple)):
                normalized[key] = list(values)
            else:
                if isinstance(values, str) and ", " in values:
                    warnings.warn(
                        f"{context}: response header {key!r} holds a flattened "
                        "comma-joined value; the original header boundaries are "
                        "lost, so it is kept as a single header entry and may "
                        "replay differently than recorded.",
                        FlattenedHeadersWarning,
                        stacklevel=2,
                    )
                normalized[key] = [values]
        return normalized
    raise ValueError(f"{context}: unrecognized response headers shape: {type(headers).__name__}")


def convert_to_bytes(resp):
    resp = convert_body_to_bytes(resp)
    return resp


def convert_to_unicode(resp):
    resp = convert_body_to_unicode(resp)
    return resp


def convert_body_to_bytes(resp):
    """
    If the request body is a string, encode it to bytes (for python3 support)

    By default yaml serializes to utf-8 encoded bytestrings.
    When this cassette is loaded by python3, it's automatically decoded
    into unicode strings. This makes sure that it stays a bytestring, since
    that's what all the internal httplib machinery is expecting.

    For more info on py3 yaml:
    http://pyyaml.org/wiki/PyYAMLDocumentation#Python3support
    """
    try:
        if resp["body"]["string"] is not None and not isinstance(resp["body"]["string"], bytes):
            resp["body"]["string"] = resp["body"]["string"].encode("utf-8")
    except (KeyError, TypeError, UnicodeEncodeError):
        # The thing we were converting either wasn't a dictionary or didn't
        # have the keys we were expecting.  Some of the tests just serialize
        # and deserialize a string.

        # Also, sometimes the thing actually is binary, so if you can't encode
        # it, just give up.
        pass
    return resp


def _convert_string_to_unicode(string):
    """
    If the string is bytes, decode it to a string (for python3 support)
    """
    result = string

    try:
        if string is not None and not isinstance(string, str):
            result = string.decode("utf-8")
    except (TypeError, UnicodeDecodeError, AttributeError):
        # Sometimes the string actually is binary or StringIO object,
        # so if you can't decode it, just give up.
        pass

    return result


def convert_body_to_unicode(resp):
    """
    If the request or responses body is bytes, decode it to a string
    (for python3 support)
    """
    if not isinstance(resp, dict):
        # Some of the tests just serialize and deserialize a string.
        return _convert_string_to_unicode(resp)
    else:
        body = resp.get("body")

        if body is not None:
            if isinstance(body, dict) and "string" in body:
                body["string"] = _convert_string_to_unicode(body["string"])
            else:
                # The thing we were converting either wasn't a dictionary or
                # didn't have the keys we were expecting.
                # For example request object has no 'string' key.
                resp["body"] = _convert_string_to_unicode(body)

    return resp
