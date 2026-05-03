"""Tests for the extended BE7200Client methods (the surface c6u does not
cover natively). Each test injects a `_MockSession` that captures the
request body sent to the router and returns a canned payload, so we can
verify both the request shape and the response parsing without touching
a live device."""

from __future__ import annotations

import unittest

from tplink_be7200.client import BE7200ApiError, BE7200Client


class _MockSession:
    """Minimal stand-in for requests.Session.

    `responses` is either a single dict (returned for every call) or a
    list of dicts consumed in FIFO order (one per .post()). `posts` is a
    log of every captured request — used by the assertions."""

    def __init__(self, responses):
        if isinstance(responses, dict):
            self._mode = 'single'
            self._payload = responses
        else:
            self._mode = 'queue'
            self._queue = list(responses)
        self.posts = []

    def post(self, url, json, timeout, verify):
        self.posts.append({'url': url, 'json': json,
                           'timeout': timeout, 'verify': verify})

        if self._mode == 'single':
            payload = self._payload
        else:
            if not self._queue:
                raise AssertionError(
                    f"_MockSession ran out of canned responses (call #{len(self.posts)} to {url})"
                )
            payload = self._queue.pop(0)

        class _Resp:
            def __init__(self, p):
                self._p = p

            def json(self):
                return self._p
        return _Resp(payload)


def _make_client(responses):
    """Build a stok-injected BE7200Client wired to a fresh mock session."""
    client = BE7200Client.from_cached_stok('192.168.1.1', 'TOK')
    client._session = _MockSession(responses)
    return client


class TestLowLevelHelpers(unittest.TestCase):
    def test_call_raises_on_non_zero_error_code(self):
        c = _make_client({'error_code': -123, 'oops': True})
        with self.assertRaises(BE7200ApiError) as ctx:
            c.call({'method': 'get'})
        self.assertEqual(ctx.exception.code, -123)

    def test_call_accepts_extra_ok_codes(self):
        c = _make_client({'error_code': -40205})
        # delete-style call: -40205 = "entry not found" should be tolerated
        result = c.call({'method': 'delete'}, ok_codes=(0, -40205))
        self.assertEqual(result['error_code'], -40205)

    def test_get_builds_correct_body_with_name(self):
        c = _make_client({'error_code': 0, 'network': {'lan': {'ipaddr': '192.168.1.1'}}})
        c.get('network', name='lan')
        self.assertEqual(c._session.posts[0]['json'], {
            'network': {'name': 'lan'}, 'method': 'get',
        })

    def test_get_builds_correct_body_with_table(self):
        c = _make_client({'error_code': 0, 'firewall': {'redirect': []}})
        c.get('firewall', table='redirect')
        self.assertEqual(c._session.posts[0]['json'], {
            'firewall': {'table': 'redirect'}, 'method': 'get',
        })

    def test_set_builds_section_payload(self):
        c = _make_client({'error_code': 0})
        c.set('network', 'lan', {'ipaddr': '192.0.2.1'})
        self.assertEqual(c._session.posts[0]['json'], {
            'network': {'lan': {'ipaddr': '192.0.2.1'}}, 'method': 'set',
        })

    def test_add_builds_table_para_payload(self):
        c = _make_client({'error_code': 0})
        c.add('ip_mac_bind', 'user_bind', 'user_bind_1',
              {'ip': '192.0.2.10', 'mac': 'aa-bb-cc-dd-ee-ff', 'hostname': 'h'})
        self.assertEqual(c._session.posts[0]['json'], {
            'ip_mac_bind': {
                'table': 'user_bind', 'name': 'user_bind_1',
                'para': {'ip': '192.0.2.10', 'mac': 'aa-bb-cc-dd-ee-ff', 'hostname': 'h'},
            },
            'method': 'add',
        })

    def test_delete_tolerates_entry_not_found(self):
        c = _make_client({'error_code': -40205})
        c.delete('ip_mac_bind', 'user_bind', 'user_bind_99')

    def test_do_builds_action_payload(self):
        c = _make_client({'error_code': 0, 'wake_on_lan': {}})
        c.do('wake_on_lan', {'wake_device': {'mac': 'aa-bb-cc-dd-ee-ff'}})
        self.assertEqual(c._session.posts[0]['json'], {
            'wake_on_lan': {'wake_device': {'mac': 'aa-bb-cc-dd-ee-ff'}},
            'method': 'do',
        })


class TestLanWan(unittest.TestCase):
    def test_get_lan_returns_lan_subdict(self):
        c = _make_client({'error_code': 0, 'network': {'lan': {
            'ipaddr': '192.168.1.1', 'netmask': '255.255.255.0', 'ip_mode': 'manual',
        }}})
        lan = c.get_lan()
        self.assertEqual(lan['ipaddr'], '192.168.1.1')

    def test_set_lan_ip_uses_manual_mode(self):
        c = _make_client({'error_code': 0})
        c.set_lan_ip('192.0.2.1')
        body = c._session.posts[0]['json']
        self.assertEqual(body['network']['lan']['ip_mode'], 'manual')
        self.assertEqual(body['network']['lan']['ipaddr'], '192.0.2.1')

    def test_set_lan_and_dhcp_atomic(self):
        c = _make_client({'error_code': 0})
        c.set_lan_and_dhcp('192.0.2.1',
                           dhcp_pool_start='192.0.2.100',
                           dhcp_pool_end='192.0.2.200',
                           dhcp_lease=7200)
        body = c._session.posts[0]['json']
        self.assertEqual(body['method'], 'set')
        self.assertEqual(body['network']['lan']['ipaddr'], '192.0.2.1')
        self.assertEqual(body['dhcpd']['udhcpd']['pool_start'], '192.0.2.100')
        self.assertEqual(body['dhcpd']['udhcpd']['lease_time'], '7200')

    def test_get_wan_status(self):
        c = _make_client({'error_code': 0, 'network': {'wan_status': {
            'ipaddr': '1.2.3.4', 'gateway': '1.2.3.1',
        }}})
        ws = c.get_wan_status()
        self.assertEqual(ws['ipaddr'], '1.2.3.4')


class TestDhcpServer(unittest.TestCase):
    def test_get_dhcp_server(self):
        c = _make_client({'error_code': 0, 'dhcpd': {'udhcpd': {
            'enable': '1', 'pool_start': '192.168.1.100',
        }}})
        d = c.get_dhcp_server()
        self.assertEqual(d['enable'], '1')

    def test_set_dhcp_server_assembles_only_provided_fields(self):
        c = _make_client({'error_code': 0})
        c.set_dhcp_server(pool_start='192.168.1.50', lease_time=3600)
        body = c._session.posts[0]['json']
        self.assertEqual(body['dhcpd']['udhcpd']['pool_start'], '192.168.1.50')
        self.assertEqual(body['dhcpd']['udhcpd']['lease_time'], '3600')
        self.assertNotIn('pool_end', body['dhcpd']['udhcpd'])

    def test_set_dhcp_server_requires_at_least_one_field(self):
        c = _make_client({'error_code': 0})
        with self.assertRaises(ValueError):
            c.set_dhcp_server()


class TestBindings(unittest.TestCase):
    def test_list_bindings_flattens_nested_response(self):
        c = _make_client({'error_code': 0, 'ip_mac_bind': {'user_bind': [
            {'user_bind_1': {'ip': '192.168.1.10', 'mac': 'aa-bb-cc-dd-ee-01', 'hostname': 'a'}},
            {'user_bind_2': {'ip': '192.168.1.11', 'mac': 'aa-bb-cc-dd-ee-02', 'hostname': 'b'}},
        ]}})
        rows = c.list_bindings()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['name'], 'user_bind_1')
        self.assertEqual(rows[1]['ip'], '192.168.1.11')

    def test_add_binding_normalizes_mac_and_returns_section_name(self):
        # First request: list_bindings (returns 0 entries); Second: add
        c = _make_client([
            {'error_code': 0, 'ip_mac_bind': {'user_bind': []}},
            {'error_code': 0},
        ])
        name = c.add_binding(ip='192.168.1.10', mac='AA:BB:CC:DD:EE:FF', hostname='h')
        self.assertEqual(name, 'user_bind_1')
        add_body = c._session.posts[1]['json']
        self.assertEqual(add_body['ip_mac_bind']['para']['mac'], 'aa-bb-cc-dd-ee-ff')

    def test_add_binding_with_explicit_index_skips_list_call(self):
        c = _make_client({'error_code': 0})
        name = c.add_binding(ip='192.168.1.10', mac='aa-bb-cc-dd-ee-ff', index=5)
        self.assertEqual(name, 'user_bind_5')
        # only the add call, no list query
        self.assertEqual(len(c._session.posts), 1)

    def test_delete_binding_uses_list_form_payload(self):
        # The web UI's actual delete payload (verified 2026-05-03 by
        # capturing the live request): name is a LIST under ip_mac_bind,
        # no `table` field. Earlier code sent name as a string with
        # `table: user_bind`; firmware misinterpreted that as "delete
        # all" and truncated the table.
        c = _make_client({'error_code': 0})
        c.delete_binding('user_bind_3')
        body = c._session.posts[0]['json']
        self.assertEqual(body['ip_mac_bind']['name'], ['user_bind_3'])
        self.assertNotIn('table', body['ip_mac_bind'])
        self.assertEqual(body['method'], 'delete')

    def test_clear_bindings_uses_one_bulk_delete(self):
        # clear_bindings now lists current bindings then sends ONE
        # bulk delete with all names — not a per-name loop.
        c = _make_client({'error_code': 0, 'ip_mac_bind': {'user_bind': [
            {'user_bind_1': {'ip': '192.0.2.1', 'mac': 'aa-bb-cc-dd-ee-01'}},
            {'user_bind_2': {'ip': '192.0.2.2', 'mac': 'aa-bb-cc-dd-ee-02'}},
        ]}})
        # second response = the delete; queue both
        c._session.payload = {'error_code': 0}
        n = c.clear_bindings()
        self.assertEqual(n, 2)
        # Two POSTs: list + bulk delete.
        self.assertEqual(len(c._session.posts), 2)
        self.assertEqual(c._session.posts[1]['json']['method'], 'delete')
        self.assertEqual(
            c._session.posts[1]['json']['ip_mac_bind']['name'],
            ['user_bind_1', 'user_bind_2'],
        )


class TestClients(unittest.TestCase):
    def test_list_clients_decodes_hostname(self):
        c = _make_client({'error_code': 0, 'hosts_info': {'online_host': [
            {'host_1': {'mac': 'aa-bb-cc-dd-ee-01', 'ip': '192.0.2.10',
                       'hostname': '%E6%B5%8B%E8%AF%95'}},
        ]}})
        rows = c.list_clients()
        self.assertEqual(rows[0]['hostname'], '测试')

    def test_list_clients_offline_uses_host_info_table(self):
        c = _make_client({'error_code': 0, 'hosts_info': {'host_info': []}})
        c.list_clients(include_offline=True)
        self.assertEqual(c._session.posts[0]['json']['hosts_info']['table'], 'host_info')


class TestWifi(unittest.TestCase):
    def test_get_wifi_returns_both_bands(self):
        c = _make_client({'error_code': 0, 'wireless': {
            'wlan_host_2g': {'ssid': 'A', 'key': 'k'},
            'wlan_host_5g': {'ssid': 'A_5G', 'key': 'k'},
        }})
        wifi = c.get_wifi()
        self.assertEqual(wifi['2g']['ssid'], 'A')
        self.assertEqual(wifi['5g']['ssid'], 'A_5G')

    def test_set_wifi_2g_only_sends_provided_fields(self):
        c = _make_client({'error_code': 0})
        c.set_wifi_2g(ssid='NewSSID')
        body = c._session.posts[0]['json']
        self.assertEqual(body['wireless']['wlan_host_2g'], {'ssid': 'NewSSID'})

    def test_set_wifi_5g_skips_when_no_fields(self):
        c = _make_client({'error_code': 0})
        c.set_wifi_5g()
        # No request should be sent
        self.assertEqual(len(c._session.posts), 0)


class TestGuest(unittest.TestCase):
    def test_get_guest_handles_missing_5g(self):
        # 4 GET probes: 2g cfg ok, 5g cfg fails, time_left_2g ok, time_left_5g fails
        c = _make_client([
            {'error_code': 0, 'guest_network': {'guest_2g': {'ssid': 'G', 'enable': '1'}}},
            {'error_code': -40101},
            {'error_code': 0, 'guest_network': {'guest_left_2g': {'time_left': '0'}}},
            {'error_code': -40101},
        ])
        g = c.get_guest()
        self.assertIsNotNone(g['2g'])
        self.assertIsNone(g['5g'])
        self.assertEqual(g['time_left_2g'], 0)
        self.assertIsNone(g['time_left_5g'])

    def test_set_guest_encodes_enable_and_encrypt(self):
        c = _make_client({'error_code': 0})
        c.set_guest(enable=True, ssid='G', key='pw', encrypt='3', band='2g')
        body = c._session.posts[0]['json']
        fields = body['guest_network']['guest_2g']
        self.assertEqual(fields['enable'], '1')
        self.assertEqual(fields['ssid'], 'G')
        self.assertEqual(fields['encrypt'], '3')

    def test_enable_disable_guest(self):
        c = _make_client([{'error_code': 0}, {'error_code': 0}])
        c.enable_guest(band='2g')
        c.disable_guest(band='2g')
        self.assertEqual(c._session.posts[0]['json']['guest_network']['guest_2g']['enable'], '1')
        self.assertEqual(c._session.posts[1]['json']['guest_network']['guest_2g']['enable'], '0')


class TestPppoe(unittest.TestCase):
    def test_get_pppoe(self):
        c = _make_client({'error_code': 0, 'protocol': {'pppoe': {'username': 'u'}}})
        self.assertEqual(c.get_pppoe()['username'], 'u')

    def test_set_pppoe_also_switches_wan_type(self):
        c = _make_client([{'error_code': 0}, {'error_code': 0}])
        c.set_pppoe(username='u', password='p', mtu=1480)
        # pppoe creds first
        first = c._session.posts[0]['json']
        self.assertEqual(first['protocol']['pppoe']['username'], 'u')
        self.assertEqual(first['protocol']['pppoe']['mtu'], '1480')
        # then wan_type=pppoe
        second = c._session.posts[1]['json']
        self.assertEqual(second['protocol']['wan']['wan_type'], 'pppoe')


class TestPortForward(unittest.TestCase):
    def test_list_port_forwards(self):
        c = _make_client({'error_code': 0, 'firewall': {'redirect': [
            {'redirect_1': {'dest_ip': '192.168.1.10', 'dest_port': '80', 'proto': 'TCP'}},
        ]}})
        rows = c.list_port_forwards()
        self.assertEqual(rows[0]['dest_port'], '80')

    def test_add_port_forward_with_explicit_index(self):
        c = _make_client({'error_code': 0})
        name = c.add_port_forward(dest_ip='192.168.1.10', dest_port=80, index=1)
        self.assertEqual(name, 'redirect_1')
        body = c._session.posts[0]['json']
        para = body['firewall']['para']
        self.assertEqual(para['dest_ip'], '192.168.1.10')
        self.assertEqual(para['dest_port'], '80')
        self.assertEqual(para['src_dport_start'], '80')

    def test_delete_port_forward(self):
        c = _make_client({'error_code': 0})
        c.delete_port_forward('redirect_2')
        self.assertEqual(c._session.posts[0]['json']['firewall']['name'], 'redirect_2')


class TestDmzDdnsUpnp(unittest.TestCase):
    def test_get_dmz(self):
        c = _make_client({'error_code': 0, 'firewall': {'dmz': {'enable': '1'}}})
        self.assertEqual(c.get_dmz()['enable'], '1')

    def test_set_dmz_enables_with_ip(self):
        c = _make_client({'error_code': 0})
        c.set_dmz(enable=True, dest_ip='192.168.1.50')
        body = c._session.posts[0]['json']
        self.assertEqual(body['firewall']['dmz']['enable'], '1')
        self.assertEqual(body['firewall']['dmz']['dest_ip'], '192.168.1.50')

    def test_set_dmz_disable_omits_ip(self):
        c = _make_client({'error_code': 0})
        c.set_dmz(enable=False)
        body = c._session.posts[0]['json']
        self.assertEqual(body['firewall']['dmz'], {'enable': '0'})

    def test_set_ddns(self):
        c = _make_client({'error_code': 0})
        c.set_ddns('user', 'pw')
        body = c._session.posts[0]['json']
        self.assertEqual(body['ddns']['phddns']['username'], 'user')

    def test_get_set_upnp(self):
        c = _make_client([
            {'error_code': 0, 'upnpd': {'config': {'enable_upnp': '1'}}},
            {'error_code': 0},
        ])
        self.assertEqual(c.get_upnp()['enable_upnp'], '1')
        c.set_upnp(False)
        self.assertEqual(c._session.posts[1]['json']['upnpd']['config']['enable_upnp'], '0')


class TestApIsolate(unittest.TestCase):
    def test_get_ap_isolate_reads_wifi(self):
        c = _make_client({'error_code': 0, 'wireless': {
            'wlan_host_2g': {'isolate': '1'},
            'wlan_host_5g': {'isolate': '0'},
        }})
        iso = c.get_ap_isolate()
        self.assertTrue(iso['2g'])
        self.assertFalse(iso['5g'])

    def test_set_ap_isolate_both_bands(self):
        c = _make_client([{'error_code': 0}, {'error_code': 0}])
        c.set_ap_isolate(isolate=True, band='both')
        self.assertEqual(c._session.posts[0]['json']['wireless']['wlan_host_2g'], {'isolate': '1'})
        self.assertEqual(c._session.posts[1]['json']['wireless']['wlan_host_5g'], {'isolate': '1'})


class TestWifiTimer(unittest.TestCase):
    def test_get_wifi_timer(self):
        c = _make_client([
            {'error_code': 0, 'time_switch': {'general': {'enable': '1'}}},
            {'error_code': 0, 'time_switch': {'time_switch': [
                {'time_switch_1': {'name': 'rule_1', 'start_time': '23:00', 'end_time': '07:00',
                                   'mon': '1', 'tue': '1', 'wed': '1', 'thu': '1', 'fri': '1',
                                   'sat': '0', 'sun': '0', 'enable': '1'}},
            ]}},
        ])
        t = c.get_wifi_timer()
        self.assertTrue(t['enable'])
        self.assertEqual(t['rules'][0]['start_time'], '23:00')

    def test_add_wifi_timer_rule_with_explicit_index(self):
        c = _make_client({'error_code': 0})
        name = c.add_wifi_timer_rule(start='23:00', end='07:00', days=['mon', 'tue'], index=1)
        self.assertEqual(name, 'time_switch_1')
        para = c._session.posts[0]['json']['time_switch']['para']
        self.assertEqual(para['mon'], '1')
        self.assertEqual(para['sat'], '0')


class TestRebootTimer(unittest.TestCase):
    def test_get_reboot_timer(self):
        c = _make_client([
            {'error_code': 0, 'reboot_timer': {'reboot_timer_status': {'enable': '0'}}},
            {'error_code': 0, 'reboot_timer': {'reboot_timer_rule': []}},
        ])
        t = c.get_reboot_timer()
        self.assertFalse(t['enable'])
        self.assertEqual(t['rules'], [])

    def test_set_reboot_timer_enable(self):
        c = _make_client({'error_code': 0})
        c.set_reboot_timer_enable(True)
        body = c._session.posts[0]['json']
        self.assertEqual(body['reboot_timer']['reboot_timer_status']['enable'], '1')

    def test_add_reboot_timer_rule(self):
        c = _make_client({'error_code': 0})
        name = c.add_reboot_timer_rule(reboot_time='03:00', days=['sun'], index=1)
        self.assertEqual(name, 'reboot_timer_rule_1')
        para = c._session.posts[0]['json']['reboot_timer']['para']
        self.assertEqual(para['reboot_time'], '03:00')
        self.assertEqual(para['sun'], '1')
        self.assertEqual(para['mon'], '0')


class TestWol(unittest.TestCase):
    def test_list_wol_devices(self):
        c = _make_client({'error_code': 0, 'wake_on_lan': {'device_list': [
            {'device_1': {'name': 'pc', 'mac': 'aa-bb-cc-dd-ee-01', 'online': '1'}},
        ]}})
        rows = c.list_wol_devices()
        self.assertEqual(rows[0]['mac'], 'aa-bb-cc-dd-ee-01')

    def test_wake_normalizes_mac(self):
        c = _make_client({'error_code': 0, 'wake_on_lan': {}})
        c.wake('AA:BB:CC:DD:EE:FF')
        body = c._session.posts[0]['json']
        self.assertEqual(body['wake_on_lan']['wake_device']['mac'], 'aa-bb-cc-dd-ee-ff')


class TestAdminLock(unittest.TestCase):
    def test_get_admin_lock_filters_zero_macs(self):
        c = _make_client({'error_code': 0, 'firewall': {'lan_manage': {
            'enable_all': '0',
            'mac1': 'aa-bb-cc-dd-ee-01', 'mac2': '00-00-00-00-00-00',
            'mac3': 'aa-bb-cc-dd-ee-02', 'mac4': '00-00-00-00-00-00',
        }}})
        a = c.get_admin_lock()
        self.assertFalse(a['enable_all'])
        self.assertEqual(len(a['allowed_macs']), 2)

    def test_set_admin_lock_pads_to_four_entries(self):
        c = _make_client({'error_code': 0})
        c.set_admin_lock(enable_all=False, allowed_macs=['AA:BB:CC:DD:EE:01'])
        body = c._session.posts[0]['json']
        fields = body['firewall']['lan_manage']
        self.assertEqual(fields['mac1'], 'aa-bb-cc-dd-ee-01')
        self.assertEqual(fields['mac4'], '00-00-00-00-00-00')


class TestSignalPower(unittest.TestCase):
    def test_get_signal_power_uses_wifi_then_power_info(self):
        c = _make_client([
            {'error_code': 0, 'wireless': {
                'wlan_host_2g': {'power': '1'},
                'wlan_host_5g': {'power': '0'},
            }},
            {'error_code': 0, 'wireless_power': {'power_info': {'power_list': ['low', 'middle', 'high']}}},
        ])
        s = c.get_signal_power()
        self.assertEqual(s['2g'], '1')
        self.assertEqual(s['5g'], '0')
        self.assertEqual(s['available'], ['low', 'middle', 'high'])

    def test_set_signal_power_translates_level(self):
        c = _make_client({'error_code': 0})
        c.set_signal_power('boost', band='both')
        body = c._session.posts[0]['json']
        self.assertEqual(body['wireless']['wlan_host_2g'], {'power': '0'})
        self.assertEqual(body['wireless']['wlan_host_5g'], {'power': '0'})

    def test_set_signal_power_unknown_level_raises(self):
        c = _make_client({'error_code': 0})
        with self.assertRaises(ValueError):
            c.set_signal_power('turbo')


class TestMacAcl(unittest.TestCase):
    def test_get_mac_acl_decodes_hostname(self):
        # GET config + DO get_white_list
        c = _make_client([
            {'error_code': 0, 'wlan_access': {'config': {'enable': '1'}}},
            {'error_code': 0, 'wlan_access': {'white_list': [
                {'white_list_1': {'mac': 'aa-bb-cc-dd-ee-01', 'name': '%E6%B5%8B%E8%AF%95'}},
            ]}},
        ])
        a = c.get_mac_acl()
        self.assertEqual(a['enable'], '1')
        self.assertEqual(a['white_list'][0]['hostname'], '测试')

    def test_set_mac_acl_mode_validates(self):
        c = _make_client({'error_code': 0})
        with self.assertRaises(ValueError):
            c.set_mac_acl_mode('blocklist')

    def test_add_mac_acl_dedupe(self):
        # First the list call returns existing entry; client returns its section without
        # making the add call.
        c = _make_client([
            {'error_code': 0, 'wlan_access': {'config': {'enable': '1'}}},
            {'error_code': 0, 'wlan_access': {'white_list': [
                {'white_list_1': {'mac': 'aa-bb-cc-dd-ee-01', 'name': 'pc'}},
            ]}},
        ])
        section = c.add_mac_acl(mac='AA:BB:CC:DD:EE:01', hostname='pc')
        self.assertEqual(section, 'white_list_1')
        self.assertEqual(len(c._session.posts), 2)  # only the get_mac_acl probes


class TestExportAll(unittest.TestCase):
    def test_export_all_collects_failures(self):
        # Every get returns an error -> export_all should not raise; each
        # entry is a dict with an 'error' key.
        c = _make_client({'error_code': -40101})
        out = c.export_all()
        for k in ('lan', 'wan', 'pppoe', 'wifi', 'bindings', 'port_forwards',
                  'dmz', 'ddns', 'upnp', 'device_info', 'sys_mode'):
            self.assertIn(k, out)
            self.assertIn('error', out[k])


if __name__ == '__main__':
    unittest.main()
