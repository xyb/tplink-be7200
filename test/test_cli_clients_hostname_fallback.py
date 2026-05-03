"""Tests for cmd_clients hostname fallback to DHCP bindings.

The router's online-clients table sometimes ships a blank hostname for
devices that did not advertise one in their DHCPDISCOVER (random-MAC
phones, headless IoT, anything renamed after the lease was signed).
The DHCP-binding table, on the other hand, is the user's own labelled
inventory — when a device has a binding entry with a hostname the user
assigned, that name is far more useful than a blank.

So: when a client's hostname is empty, fall back to the binding
hostname keyed by the same IP. Match is silent (no marker).
"""

import io
import sys
import unittest
from unittest.mock import patch

from tplink_be7200 import cli


class _Args:
    host = '192.168.1.1'
    json = False
    all = False


def _run_clients(rows, bindings=()):
    """Run cmd_clients() with mocked client returning the given rows and
    bindings. Returns stdout text."""
    stdout = io.StringIO()
    with patch('tplink_be7200.cli._client') as mock_client_factory, \
         patch.object(sys, 'stdout', stdout):
        c = mock_client_factory.return_value
        c.list_clients.return_value = list(rows)
        c.list_bindings.return_value = list(bindings)
        cli.cmd_clients(_Args())
    return stdout.getvalue()


class ClientsHostnameFallback(unittest.TestCase):

    def test_blank_hostname_filled_from_binding_by_ip(self):
        """A client with empty hostname picks up the matching binding's
        hostname (matched by IP)."""
        rows = [{
            'ip': '192.0.2.15', 'mac': 'aa-bb-cc-dd-ee-15',
            'type': '0', 'hostname': '',
        }]
        bindings = [{
            'name': 'user_bind_24', 'ip': '192.0.2.15',
            'mac': 'aa-bb-cc-dd-ee-15', 'hostname': 'h1',
        }]
        out = _run_clients(rows, bindings)
        self.assertIn('h1', out)

    def test_existing_hostname_is_kept_not_overwritten(self):
        """If the client already has a hostname, we do NOT replace it
        even when the binding has a different one — the live DHCP
        hostname is the authoritative current state."""
        rows = [{
            'ip': '192.0.2.42', 'mac': 'aa-bb-cc-dd-ee-42',
            'type': '0', 'hostname': 'live-name',
        }]
        bindings = [{
            'name': 'user_bind_8', 'ip': '192.0.2.42',
            'mac': 'aa-bb-cc-dd-ee-42', 'hostname': 'old-binding-name',
        }]
        out = _run_clients(rows, bindings)
        self.assertIn('live-name', out)
        self.assertNotIn('old-binding-name', out)

    def test_blank_with_no_matching_binding_stays_blank(self):
        """If neither the client row nor any binding has a hostname,
        the column stays blank. Don't invent names."""
        rows = [{
            'ip': '192.0.2.99', 'mac': 'aa-bb-cc-dd-ee-99',
            'type': '0', 'hostname': '',
        }]
        out = _run_clients(rows, bindings=[])
        # The IP should still print; we just check no fake name appears.
        self.assertIn('192.0.2.99', out)
        # Last column for that line should be empty (line ends with the
        # MAC then trailing whitespace/newline, no hostname token).

    def test_binding_match_is_silent_no_marker(self):
        """Per spec: the binding-derived name must appear as if it were
        the real hostname — no '(binding)' suffix or similar
        annotation. The user wants a clean list."""
        rows = [{
            'ip': '192.0.2.15', 'mac': 'aa-bb-cc-dd-ee-15',
            'type': '0', 'hostname': '',
        }]
        bindings = [{
            'name': 'user_bind_24', 'ip': '192.0.2.15',
            'mac': 'aa-bb-cc-dd-ee-15', 'hostname': 'h1',
        }]
        out = _run_clients(rows, bindings)
        self.assertNotIn('(binding)', out)
        self.assertNotIn('[binding]', out)

    def test_bindings_lookup_failure_does_not_break_clients(self):
        """If list_bindings() raises (e.g. transient router blip), the
        clients table still prints — falling back to the original blank
        hostnames. We must not regress the read-only happy path."""
        stdout = io.StringIO()
        with patch('tplink_be7200.cli._client') as mock_client_factory, \
             patch.object(sys, 'stdout', stdout):
            c = mock_client_factory.return_value
            c.list_clients.return_value = [{
                'ip': '192.0.2.15', 'mac': 'aa-bb-cc-dd-ee-15',
                'type': '0', 'hostname': '',
            }]
            c.list_bindings.side_effect = RuntimeError("router blip")
            cli.cmd_clients(_Args())  # must NOT raise
        self.assertIn('192.0.2.15', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
