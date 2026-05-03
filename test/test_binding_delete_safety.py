"""Tests for the safe payload formats discovered by capturing the
TL-7DR7270 web UI's actual binding API requests (2026-05-03).

Two contracts pinned here:

1. ``delete_binding(name)`` sends ``{ip_mac_bind: {name: [name]},
   method: "delete"}`` — the LIST form. Earlier versions sent ``name``
   as a string with a ``table: "user_bind"`` field; the firmware
   misread that as "delete all" and truncated the table. The web UI
   never uses that format; we now match what the UI sends.

2. ``update_binding(name, ip=, mac=, hostname=)`` sends ``{ip_mac_bind:
   {<name>: {ip, mac, hostname}}, method: "set"}`` — in-place rename
   without involving delete. The binding name is a *key*, not a
   field.
"""

import unittest
from unittest.mock import MagicMock

from tplink_be7200.client import BE7200Client


class _StubResp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p


def _client_with(*payloads):
    c = BE7200Client.from_cached_stok('192.168.1.1', 'TOK')
    queue = list(payloads)
    sess = MagicMock()
    def fake_post(url, json=None, timeout=None, verify=None):
        return _StubResp(queue.pop(0))
    sess.post.side_effect = fake_post
    c._session = sess
    return c


class DeleteBindingPayload(unittest.TestCase):

    def test_single_delete_uses_list_form_no_table(self):
        """The web UI's wire format. Anything else risks the firmware
        truncate misbehavior we hit on 2026-05-03."""
        c = _client_with({'error_code': 0})
        c.delete_binding('user_bind_3')
        sent = c._session.post.call_args.kwargs['json']
        self.assertEqual(sent, {
            'ip_mac_bind': {'name': ['user_bind_3']},
            'method': 'delete',
        })

    def test_single_delete_tolerates_entry_not_found(self):
        """The web UI accepts -40205 silently (idempotent delete);
        we mirror that — it must not surface as an exception."""
        c = _client_with({'error_code': -40205})
        c.delete_binding('user_bind_999')  # must NOT raise

    def test_bulk_delete_with_multiple_names(self):
        """The 删除所选 ("delete selected") flow passes every checked
        name in one list — one round trip, not N."""
        c = _client_with({'error_code': 0})
        c.delete_bindings(['user_bind_3', 'user_bind_7', 'user_bind_22'])
        sent = c._session.post.call_args.kwargs['json']
        self.assertEqual(sent['ip_mac_bind']['name'],
                         ['user_bind_3', 'user_bind_7', 'user_bind_22'])
        self.assertEqual(sent['method'], 'delete')


class UpdateBindingPayload(unittest.TestCase):

    def test_update_uses_set_with_name_as_key(self):
        """The web UI's edit→save flow. Note the binding name is the
        KEY under ip_mac_bind, not a `name` field."""
        c = _client_with({'error_code': 0})
        c.update_binding(
            'user_bind_5',
            ip='192.168.1.10',
            mac='aa-bb-cc-dd-ee-ff',
            hostname='my-host',
        )
        sent = c._session.post.call_args.kwargs['json']
        self.assertEqual(sent, {
            'ip_mac_bind': {'user_bind_5': {
                'ip': '192.168.1.10',
                'mac': 'aa-bb-cc-dd-ee-ff',
                'hostname': 'my-host',
            }},
            'method': 'set',
        })

    def test_update_normalizes_mac_separator(self):
        """Routers reject colon-separated MACs in this endpoint; the
        rest of the SDK already lowercases and dashes them, do the
        same here for consistency."""
        c = _client_with({'error_code': 0})
        c.update_binding(
            'user_bind_5',
            ip='192.168.1.10',
            mac='AA:BB:CC:DD:EE:FF',
            hostname='h',
        )
        sent = c._session.post.call_args.kwargs['json']
        self.assertEqual(
            sent['ip_mac_bind']['user_bind_5']['mac'],
            'aa-bb-cc-dd-ee-ff',
        )


class RebuildBindings(unittest.TestCase):

    def test_rebuild_lists_then_bulk_deletes_then_adds(self):
        """rebuild_bindings = list + one bulk delete + N adds. The
        bulk delete uses the safe list form; no per-row deletes."""
        records = [
            {'ip': '192.0.2.42', 'mac': 'aa-bb-cc-dd-ee-01', 'hostname': 'h1'},
        ]
        c = _client_with(
            # 1) list_bindings
            {'error_code': 0, 'ip_mac_bind': {'user_bind': [
                {'user_bind_1': {}}, {'user_bind_2': {}},
            ]}},
            # 2) bulk delete
            {'error_code': 0},
            # 3) list_bindings (for add_binding's index pick)
            {'error_code': 0, 'ip_mac_bind': {'user_bind': []}},
            # 4) add h1
            {'error_code': 0},
        )
        result = c.rebuild_bindings(records)
        self.assertEqual(result, 1)
        sent = [call.kwargs['json'] for call in c._session.post.call_args_list]
        self.assertEqual(sent[1]['method'], 'delete')
        self.assertEqual(
            sent[1]['ip_mac_bind']['name'],
            ['user_bind_1', 'user_bind_2'],
        )
        self.assertEqual(sent[3]['method'], 'add')
        self.assertEqual(sent[3]['ip_mac_bind']['para']['hostname'], 'h1')


if __name__ == '__main__':
    unittest.main()
