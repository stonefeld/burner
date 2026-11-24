"""Tests for the pure logic. Whatever touches KVM, nft or GitHub is tested with `burner selftest` on a host."""
import json
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import burner

REPO = '[[repos]]\nname = "o/r"\napp_id = 1\ninstallation_id = 2\nkey = "/x.pem"\n'


def config(extra=''):
    with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as f:
        f.write(extra+'\n'+REPO)
    return burner.load(f.name)


class Configuration(unittest.TestCase):
    def test_defaults_and_env_merge(self):
        cfg = config('[cache.env]\nCARGO_HOME = "/cache/runner/cargo"\n[slots]\ncount = 2')
        self.assertEqual(cfg['slots']['count'], 2)
        self.assertEqual(cfg['cache']['env']['CARGO_HOME'], '/cache/runner/cargo')
        self.assertIn('XDG_CACHE_HOME', cfg['cache']['env'])
        self.assertEqual(cfg['repos'][0]['trusted_branches'], ['main'])

    def test_rejects_dangerous_values(self):
        for bad in ('[network]\nsubnet = "8.8.0.0/16"', '[network]\ndns = ["192.168.0.1"]',
                    '[vm]\npackages = ["x; rm -rf /"]', '[network]\nodd_key = 1'):
            with self.assertRaises(SystemExit, msg=bad):
                config(bad)

    def test_one_network_per_slot(self):
        cfg = config('[slots]\ncount = 3')
        n0, n3 = burner.net(cfg, 0), burner.net(cfg, 3)
        self.assertEqual((n0['host'], n0['guest'], n0['bridge']), ('10.203.0.1', '10.203.0.10', 'burner0'))
        self.assertEqual((n3['host'], n3['guest'], n3['bridge']), ('10.203.3.1', '10.203.3.10', 'burner3'))
        self.assertNotEqual(n0['mac'], n3['mac'])


class Filter(unittest.TestCase):
    def test_rules(self):
        nft = burner.nft_rules(config('[slots]\ncount = 2\n[network]\nntp = false\nblock = ["203.0.113.7/32"]'))
        self.assertIn('"burner0" . 10.203.0.10, "burner1" . 10.203.1.10, "burner2" . 10.203.2.10', nft)
        self.assertIn('203.0.113.7/32', nft)
        self.assertIn('ip daddr { 1.1.1.1, 8.8.8.8 } meta l4proto { tcp, udp } th dport 53 counter accept', nft)
        self.assertNotIn('dport 123', nft)
        self.assertIn('oifname "tailscale0" counter jump deny', nft)
        chain = nft.split('chain from_vm')[1].split('\n    }')[0]
        self.assertEqual(chain.strip().splitlines()[-1].strip(),
                         'counter jump deny comment "port or protocol not allowed"')

    def test_normalize_ignores_declaration_order_and_counters(self):
        a = {'nftables': [{'metainfo': {}}, {'set': {'name': 'b', 'elem': [2, 1]}}, {'chain': {'name': 'a'}},
                          {'rule': {'chain': 'a', 'expr': [{'counter': {'packets': 5}}], 'handle': 9}},
                          {'rule': {'chain': 'a', 'expr': ['x']}}]}
        b = {'nftables': [{'chain': {'name': 'a'}}, {'set': {'name': 'b', 'elem': [1, 2]}},
                          {'rule': {'chain': 'a', 'expr': [{'counter': {'packets': 0}}], 'handle': 1}},
                          {'rule': {'chain': 'a', 'expr': ['x']}}]}
        self.assertEqual(burner.normalize(a), burner.normalize(b))
        c = json.loads(json.dumps(b))
        c['nftables'][2], c['nftables'][3] = c['nftables'][3], c['nftables'][2]
        self.assertNotEqual(burner.normalize(b), burner.normalize(c))

    def test_wan_left_out_of_the_comparison(self):
        a = {'nftables': [{'set': {'name': 'wan', 'elem': ['1.2.3.4']}}]}
        b = {'nftables': [{'set': {'name': 'wan'}}]}
        self.assertEqual(burner.normalize(burner.without_wan(a)), burner.normalize(b))

    def test_drop_counters_by_comment(self):
        ruleset = {'nftables': [
            {'rule': {'comment': 'private destination', 'expr': [{'counter': {'packets': 3}}, {'jump': {}}]}},
            {'rule': {'comment': 'private destination', 'expr': [{'counter': {'packets': 2}}]}},
            {'rule': {'expr': [{'counter': {'packets': 99}}]}}]}
        self.assertEqual(burner.drop_counters(ruleset), {'private destination': 5})


class Domain(unittest.TestCase):
    def test_no_shared_devices_and_l2_filter(self):
        cfg = config()
        x = ET.fromstring(burner.domain_xml(cfg, 'd', '00000000-0000-0000-0000-000000000001', 1,
                                            '/d.qcow2', '/c.qcow2', '/s.iso'))
        self.assertFalse(x.findall('./devices/filesystem') or x.findall('./devices/hostdev'))
        nic = x.find('./devices/interface')
        self.assertEqual(nic.find('filterref').get('filter'), 'burner-l2')
        self.assertEqual(nic.find('filterref/parameter').get('value'), '10.203.1.10')
        serials = {d.find('target').get('dev'): getattr(d.find('serial'), 'text', None)
                   for d in x.findall('./devices/disk')}
        self.assertEqual(serials['vdb'], 'burner-cache')
        self.assertEqual(x.find('cputune/global_quota').text, '200000')

    def test_user_data_has_verified_runner_and_provision(self):
        with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False) as p:
            p.write('#!/bin/sh\necho hi\n')
        cfg = config(f'[vm]\nprovision = "{p.name}"')
        ud = burner.base_user_data(cfg, {'url': 'https://u', 'sha256': 'a'*64})
        paths = {f['path'] for f in ud['write_files']}
        self.assertIn('/usr/local/lib/burner/provision.sh', paths)
        self.assertIn('/etc/systemd/system/docker.service.d/burner.conf', paths)
        self.assertEqual(ud['users'], [])
        self.assertIn('sha256sum -c', burner.PREPARE)


def run_(event='push', branch='main', repo='o/r', path='.github/workflows/ci.yml'):
    return {'event': event, 'head_branch': branch, 'head_repository': {'full_name': repo}, 'path': path}


class Trust(unittest.TestCase):
    def setUp(self):
        self.rc = config()['repos'][0]

    def test_admission(self):
        job = {'status': 'queued', 'labels': ['self-hosted', 'burner']}
        self.assertTrue(burner.admissible(run_('pull_request', 'feature'), job, self.rc))
        self.assertFalse(burner.admissible(run_(repo='someone/fork'), job, self.rc))
        self.assertFalse(burner.admissible(run_(), {**job, 'labels': ['self-hosted', 'burner', 'gpu']}, self.rc))
        self.assertFalse(burner.admissible(run_(), {**job, 'labels': ['ubuntu-latest']}, self.rc))
        self.assertFalse(burner.admissible(run_(), {**job, 'status': 'in_progress'}, self.rc))
        self.rc['workflows'] = ['.github/workflows/other.yml']
        self.assertFalse(burner.admissible(run_(), job, self.rc))

    def test_only_merged_successful_code_promotes(self):
        ok = {'conclusion': 'success'}
        self.assertTrue(burner.trusted(run_(), ok, self.rc))
        self.assertTrue(burner.trusted(run_('schedule'), ok, self.rc))
        self.assertFalse(burner.trusted(run_('pull_request'), ok, self.rc))
        self.assertFalse(burner.trusted(run_(branch='feature'), ok, self.rc))
        self.assertFalse(burner.trusted(run_(repo='someone/fork'), ok, self.rc))
        self.assertFalse(burner.trusted(run_(), {'conclusion': 'failure'}, self.rc))


class Images(unittest.TestCase):
    def test_prune_keeps_current_and_live_backings(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            burner.STATE = d/'state'
            burner.STATE.mkdir()
            folder = d/'caches'
            folder.mkdir()
            old = [folder/f'cache-{n}.qcow2' for n in range(4)]
            for f in old:
                f.touch()
            burner.point(folder/'current', old[3])
            burner.write(burner.STATE/'slot-1.json', {'cache_base': str(old[1].resolve())})
            burner.prune(folder, 'cache')
            self.assertEqual(sorted(p.name for p in folder.glob('cache-*')), ['cache-1.qcow2', 'cache-3.qcow2'])


class Monitor(unittest.TestCase):
    def snapshot(self, **slot):
        now = time.time()
        return {'time': now, 'host': 'h', 'active': True, 'cpu_total': 100, 'cpu_idle': 50, 'threads': 4,
                'load': 1.0, 'mem_total': 16 << 30, 'mem_available': 8 << 30, 'disk_free': 100 << 30,
                'uptime': 3600, 'policy_ok': True, 'drops': {'private destination': 7}, 'wan': {},
                'base': {'file': 'base-1.qcow2', 'time': now-86400}, 'building': {}, 'selftest': {},
                'caches': {'o/r': None},
                'slots': {0: {'running': False, 'cpu_usec': 0, 'mem_bytes': 0, 'out_bytes': 0, 'in_bytes': 0},
                          1: {'running': True, 'cpu_usec': 0, 'mem_bytes': 1 << 30, 'out_bytes': 0,
                              'in_bytes': 0, 'phase': 'busy', 'repo': 'o/r', 'created': now-60, **slot}}}

    def test_rates_between_samples(self):
        old = self.snapshot()
        new = self.snapshot(cpu_usec=4_000_000, out_bytes=2048, in_bytes=4096)
        new['time'] = old['time']+2
        new['cpu_total'], new['cpu_idle'] = 200, 75
        burner.with_rates(new, old)
        self.assertEqual(new['cpu_pct'], 75.0)
        self.assertEqual(new['slots'][1]['cpu_pct'], 200.0)  # two vCPUs fully busy
        self.assertEqual((new['slots'][1]['out_bps'], new['slots'][1]['in_bps']), (1024, 2048))

    def test_render_from_json_has_no_secrets_and_survives_string_keys(self):
        s = json.loads(json.dumps(self.snapshot()))
        text = burner.render(s, burner.Paint(False))
        self.assertIn('busy', text)
        self.assertIn('empty: next trusted job fills it', text)
        self.assertNotIn('maint', text)  # idle maintenance slot is hidden
        net = burner.render(s, burner.Paint(False), network=True)
        self.assertIn('private destination', net)

    def test_printable_strips_terminal_escapes(self):
        self.assertEqual(burner.printable('ok\x1b]0;pwned\x07\n'), 'ok?]0;pwned?\n')


if __name__ == '__main__':
    unittest.main()
