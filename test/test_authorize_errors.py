"""Tests for TPLinkXDRClient.authorize() error handling.

The router's login endpoint is failure-prone in two specific ways:

  1. After enough wrong-password attempts the router returns
     ``error_code=-40401`` with a remaining-attempts counter. Web UI
     login does NOT reset this counter, so blindly retrying the CLI
     can permanently lock the device.

  2. When the rate-limit threshold is reached the router stops returning
     JSON entirely and starts serving an HTML 5xx page (typically 502).
     Older library code raised a raw ``json.JSONDecodeError`` which gave
     no actionable hint.

These tests pin the contract: authorize() must raise ``ClientException``
with a message that surfaces *what actually happened* in each case, so a
human reading the stderr can decide whether to wait, fix the password,
or escalate.
"""

import unittest
from collections import deque

from tplinkrouterc6u.common.exception import ClientException

from tplink_be7200.client import BE7200Client


class _Resp:
    """requests.Response stand-in covering everything authorize() reads."""

    def __init__(self, *, status=200, json_payload=None, text="", reason=""):
        self.status_code = status
        self._json = json_payload
        self.text = text
        self.reason = reason

    def json(self):
        if self._json is None:
            raise ValueError("No JSON object could be decoded")
        return self._json


class _QueueSession:
    """Replay a fixed sequence of responses against successive .post() calls."""

    def __init__(self, responses):
        self._queue = deque(responses)

    def post(self, url, json, timeout, verify):
        if not self._queue:
            raise AssertionError("unexpected extra POST: %r" % (json,))
        return self._queue.popleft()


def _client_with_responses(*responses):
    """Build a BE7200Client whose session yields the given responses in order.

    The first response feeds the get_encrypt_info probe; subsequent ones
    feed the actual login POST and any retries. A baseline encrypt-info
    response is prepended so tests only need to describe the login leg.
    """
    encrypt_info = _Resp(json_payload={
        'error_code': 0,
        'nonce': 'TESTNONCE',
        'encrypt_type': ['3'],
    })
    client = BE7200Client('192.168.1.1', 'pw')
    client._session = _QueueSession([encrypt_info, *responses])
    return client


class AuthorizeErrorMessages(unittest.TestCase):

    def test_502_with_empty_body_explains_rate_limit(self):
        """A bare 502 (router throttled the endpoint) must NOT surface as
        a JSON decode crash — it should call out the HTTP status, the
        rate-limit hypothesis, and tell the user to wait."""
        client = _client_with_responses(
            _Resp(status=502, reason='Bad Gateway', text=''),
        )

        with self.assertRaises(ClientException) as ctx:
            client.authorize()

        msg = str(ctx.exception)
        self.assertIn('HTTP 502', msg)
        self.assertIn('Bad Gateway', msg)
        self.assertIn('rate-limited', msg.lower())
        self.assertNotIn('JSONDecodeError', msg)  # raw decoder error must not leak

    def test_502_with_html_body_truncates_and_quotes_body(self):
        """When the router returns an HTML error page, the message should
        include a short, quoted snippet so the operator can recognize it
        without reading the full payload."""
        html = '<html><body>502 Bad Gateway</body></html>' + ('x' * 1000)
        client = _client_with_responses(
            _Resp(status=502, reason='Bad Gateway', text=html),
        )

        with self.assertRaises(ClientException) as ctx:
            client.authorize()

        msg = str(ctx.exception)
        self.assertIn('HTTP 502', msg)
        self.assertIn('502 Bad Gateway', msg)  # snippet preserved
        self.assertLess(len(msg), len(html))   # but not the whole 1KB

    def test_minus_40401_reports_attempt_counter_and_warns_about_lockout(self):
        """The crucial -40401 case: the message must spell out the
        remaining-attempts ratio and warn that web UI login does NOT
        reset it, otherwise the user will exhaust the last try."""
        client = _client_with_responses(
            _Resp(status=401, json_payload={
                'error_code': -40401,
                'data': {'code': -40401, 'max_time': 20, 'time': 19, 'group': 0},
            }),
        )

        with self.assertRaises(ClientException) as ctx:
            client.authorize()

        msg = str(ctx.exception)
        self.assertIn('-40401', msg)
        # Either "19/20" or "19" + "20" both showing the danger zone.
        self.assertIn('19', msg)
        self.assertIn('20', msg)
        self.assertIn('Web UI', msg)
        self.assertIn('lock', msg.lower())

    def test_missing_stok_is_explicit_not_a_keyerror(self):
        """A 200 response that lacks 'stok' must produce a clear message,
        not a bare KeyError leaking up to the caller."""
        client = _client_with_responses(
            _Resp(status=200, json_payload={'error_code': 0}),
        )

        with self.assertRaises(ClientException) as ctx:
            client.authorize()

        self.assertIn('missing stok', str(ctx.exception))

    def test_non_dict_json_is_rejected(self):
        """If the router ever returned a list (or null) the legacy code
        would crash later with an obscure error; reject it up front."""
        client = _client_with_responses(
            _Resp(status=200, json_payload=['unexpected', 'list']),
        )

        with self.assertRaises(ClientException) as ctx:
            client.authorize()

        self.assertIn('Unexpected', str(ctx.exception))

    def test_happy_path_sets_stok(self):
        """Sanity check: a normal success response still sets _stok and
        does not raise — the new error branches must not break the
        common case."""
        client = _client_with_responses(
            _Resp(status=200, json_payload={'error_code': 0, 'stok': 'TOKABC'}),
        )

        client.authorize()

        self.assertEqual(client._stok, 'TOKABC')


if __name__ == '__main__':
    unittest.main()
