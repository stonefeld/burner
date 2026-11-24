#!/usr/bin/env python3
"""burner: one GitHub Actions job per throwaway Ubuntu VM, with a trusted per-repo cache.

Runs on a Linux host with KVM/libvirt. Standard library only. See README.md.
"""
import argparse
import base64
import contextlib
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

CONFIG = Path(os.environ.get('BURNER_CONFIG', '/etc/burner/burner.toml'))
STATE = Path('/var/lib/burner')
DISKS = Path('/var/lib/libvirt/images/burner')
DOWNLOADS = Path('/var/cache/burner')
RUN = Path('/run/burner')
FIREWALL = Path('/etc/burner/firewall.nft')
GROUP = 'burner'  # operators: read the status snapshot, never need sudo to watch

NON_PUBLIC = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
              '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15',
              '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4']
GITHUB_LABELS = {'self-hosted', 'linux', 'x64'}
TRUSTED_EVENTS = {'push', 'schedule', 'workflow_dispatch'}
# A runner that has not picked a job yet: counts against the queue when admitting.
WAITING_PHASES = {'reserved', 'creating', 'ready', 'registered'}

DEFAULTS = {
    'vm': {'ubuntu': 'noble', 'vcpus': 2, 'memory_gib': 8, 'disk_gib': 60, 'cpu_quota': 2.0,
           'packages': ['git', 'make', 'curl', 'jq', 'unzip', 'zip', 'zstd', 'build-essential',
                        'pkg-config', 'python3-venv'],
           'provision': ''},
    'slots': {'count': 1, 'timeout_min': 45, 'idle_timeout_s': 180},
    'network': {'subnet': '10.203.0.0/16', 'bridge_prefix': 'burner', 'dns': ['1.1.1.1', '8.8.8.8'],
                'tcp_ports': [80, 443], 'ntp': True, 'block': [],
                'forbidden_interfaces': ['tailscale0'],
                'wan_observers': ['https://api.ipify.org', 'https://checkip.amazonaws.com']},
    'cache': {'gib': 80, 'max_gib': 60, 'buildkit_gib': 20, 'work_dir': '/cache/runner/work',
              'env': {'AGENT_TOOLSDIRECTORY': '/cache/runner/toolcache',
                      'XDG_CACHE_HOME': '/cache/runner/xdg',
                      'npm_config_cache': '/cache/runner/npm',
                      'npm_config_store_dir': '/cache/runner/pnpm-store',
                      'PLAYWRIGHT_BROWSERS_PATH': '/cache/runner/ms-playwright'}},
}
REPO_DEFAULTS = {'label': 'burner', 'trusted_branches': ['main'], 'workflows': [], 'heartbeat': ''}


# ---------------------------------------------------------------- configuration

def load(path=CONFIG):
    data = tomllib.loads(Path(path).read_text())
    cfg = json.loads(json.dumps(DEFAULTS))
    for section, values in data.items():
        if section == 'repos':
            continue
        if section not in cfg:
            raise SystemExit(f'Unknown section in {path}: [{section}]')
        for key, value in values.items():
            if key not in cfg[section]:
                raise SystemExit(f'Unknown key in {path}: {section}.{key}')
            current = cfg[section][key]
            cfg[section][key] = {**current, **value} if isinstance(current, dict) else value
    cfg['repos'] = [{**REPO_DEFAULTS, **r} for r in data.get('repos', [])]
    validate(cfg)
    return cfg


def validate(cfg):
    def require(condition, message):
        if not condition:
            raise SystemExit('Invalid configuration: '+message)
    s, n, c = cfg['slots'], cfg['network'], cfg['cache']
    require(1 <= s['count'] <= 32, 'slots.count must be between 1 and 32')
    subnet = ipaddress.ip_network(n['subnet'])
    require(subnet.is_private and subnet.prefixlen <= 24 - s['count'].bit_length(),
            'network.subnet must be private with room for one /24 per slot plus maintenance')
    require(re.fullmatch(r'[a-z][a-z0-9]{0,10}', n['bridge_prefix']), 'network.bridge_prefix: lowercase, up to 11')
    for ip in n['dns']:
        require(ipaddress.ip_address(ip).is_global, 'network.dns must be public resolvers')
    for net in n['block']:
        ipaddress.ip_network(net)
    require(all(re.fullmatch(r'[a-z0-9][a-z0-9+.:-]*', p) for p in cfg['vm']['packages']),
            'vm.packages must be apt package names')
    require(not cfg['vm']['provision'] or Path(cfg['vm']['provision']).is_file(),
            'vm.provision points to a missing file')
    require(c['max_gib'] <= c['gib'], 'cache.max_gib cannot exceed cache.gib')
    for k, v in c['env'].items():
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) and '\n' not in v, f'cache.env.{k}')
    require(cfg['repos'], 'at least one [[repos]] is required')
    names = [rc.get('name', '') for rc in cfg['repos']]
    require(len(set(names)) == len(names), 'duplicated repos')
    for rc in cfg['repos']:
        require(re.fullmatch(r'[\w.-]+/[\w.-]+', rc.get('name', '')), 'repos.name is owner/repo')
        require(all(k in rc for k in ('app_id', 'installation_id', 'key')),
                f'{rc["name"]}: app_id, installation_id and key are required')
        rc['label'] = rc['label'].lower()
        require(rc['label'] not in GITHUB_LABELS, f'{rc["name"]}: label must be your own')


def net(cfg, i):
    """Slot i gets its own /24 and its own bridge: guests never share L2."""
    block = next(x for j, x in enumerate(ipaddress.ip_network(cfg['network']['subnet']).subnets(new_prefix=24))
                 if j == i)
    return {'name': f'burner-{i}', 'bridge': f"{cfg['network']['bridge_prefix']}{i}", 'host': str(block[1]),
            'guest': str(block[10]), 'mac': f'52:54:00:6b:{i // 256:02x}:{i % 256:02x}'}


def repo(cfg, name):
    rc = next((rc for rc in cfg['repos'] if rc['name'] == name), None)
    if rc is None:
        raise RuntimeError(f'{name} is not in the configuration.')
    return rc


def slug(name):
    return name.replace('/', '__')


# ---------------------------------------------------------------- helpers

def run(*args, timeout=120, stdin=None):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=timeout, input=stdin).stdout.strip()


def virsh(*args, timeout=120):
    return run('virsh', '-c', 'qemu:///system', *args, timeout=timeout)


def exists(domain):
    return domain in virsh('list', '--all', '--name').splitlines()


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def write(path, data, mode=0o600):
    path = Path(path)
    tmp = path.with_name('.'+path.name+'.tmp')
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.chmod(tmp, mode)
    tmp.replace(path)
    return path


def event(phase, **data):
    print(json.dumps({'event': phase, **data}), flush=True)


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def kvm_owned(path, mode):
    os.chown(path, 0, grp.getgrnam('kvm').gr_gid)
    os.chmod(path, mode)


def kvm_dir(path):
    # With UMask=0077, mkdir(mode=0750) alone does not let QEMU traverse the folder.
    path.mkdir(parents=True, exist_ok=True)
    kvm_owned(path, 0o750)
    return path


def overlay(backing, target, gib):
    run('qemu-img', 'create', '-f', 'qcow2', *(('-F', 'qcow2', '-b', str(backing)) if backing else ()),
        str(target), f'{gib}G')
    os.chown(target, pwd.getpwnam('libvirt-qemu').pw_uid, grp.getgrnam('kvm').gr_gid)
    os.chmod(target, 0o600)


def seal_image(source, folder, prefix):
    """Flattens source into folder/<prefix>-<ts>.qcow2, read-only, and returns it."""
    kvm_dir(folder)
    final = folder/f'{prefix}-{int(time.time())}.qcow2'
    partial = final.with_suffix('.partial')
    partial.unlink(missing_ok=True)
    run('qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', str(source), str(partial), timeout=3600)
    run('qemu-img', 'check', str(partial), timeout=600)
    kvm_owned(partial, 0o440)
    partial.rename(final)
    return final


def point(link, target):
    tmp = link.with_name('.'+link.name+'.tmp')
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target.name)
    tmp.replace(link)


def current(link):
    return link.resolve() if link.is_symlink() and link.resolve().exists() else None


def prune(folder, prefix):
    """Deletes images that are neither current nor backing a live slot."""
    used = {str(current(folder/'current'))}
    for state in STATE.glob('slot-*.json'):
        used |= {v for k, v in read(state).items() if k in ('base', 'cache_base')}
    for image in folder.glob(prefix+'-*.qcow2'):
        if str(image.resolve()) not in used:
            image.unlink()


@contextlib.contextmanager
def lock(name, wait=True):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(STATE/f'{name}.lock', 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def printable(text):
    """Guest output is untrusted: no terminal escape sequences reach the operator."""
    return ''.join(ch if ch in '\n\t' or ch.isprintable() else '?' for ch in text)


# ---------------------------------------------------------------- guest agent

def qga(domain, command, secret=False, timeout=30):
    text = json.dumps(command)
    if secret:
        # Through virsh stdin: an argument would be visible in /proc to the whole host.
        out = run('virsh', '-q', '-c', 'qemu:///system', stdin=f"qemu-agent-command {domain} '{text}'\n",
                  timeout=timeout)
        out = next(line for line in out.splitlines() if line.startswith('{'))
    else:
        out = virsh('qemu-agent-command', domain, text, timeout=timeout)
    return json.loads(out)['return']


def guest(domain, argv, limit=120, codes=(0,), stdin=None, wait=True):
    args = {'path': argv[0], 'arg': list(argv[1:]), 'capture-output': wait}
    if stdin is not None:
        args['input-data'] = base64.b64encode(stdin.encode()).decode()
    pid = qga(domain, {'execute': 'guest-exec', 'arguments': args}, secret=stdin is not None)['pid']
    if not wait:
        return pid
    deadline = time.monotonic()+limit
    while time.monotonic() < deadline:
        r = qga(domain, {'execute': 'guest-exec-status', 'arguments': {'pid': pid}})
        if r.get('exited'):
            out = base64.b64decode(r.get('out-data', '')).decode(errors='replace')
            out += base64.b64decode(r.get('err-data', '')).decode(errors='replace')
            if codes is not None and r.get('exitcode') not in codes:
                raise RuntimeError(f'{argv[0]} exited {r.get("exitcode")} in {domain}:\n{printable(out[-2000:])}')
            return out
        time.sleep(2)
    raise RuntimeError(f'{argv[0]} did not finish within {limit}s in {domain}.')


def wait_for_agent(domain, limit):
    deadline = time.monotonic()+limit
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            qga(domain, {'execute': 'guest-ping'}, timeout=5)
            return
        time.sleep(3)
    raise RuntimeError(f'The guest agent of {domain} did not answer within {limit}s.')


def shut_down(domain, limit=120):
    with contextlib.suppress(Exception):
        virsh('shutdown', domain, '--mode', 'agent')
    deadline = time.monotonic()+limit
    while time.monotonic() < deadline:
        if virsh('domstate', domain) == 'shut off':
            return True
        time.sleep(2)
    return False


# ---------------------------------------------------------------- network and filter

def nft_rules(cfg):
    n = cfg['network']
    p = n['bridge_prefix']
    origin = ', '.join(f'"{net(cfg, i)["bridge"]}" . {net(cfg, i)["guest"]}' for i in range(cfg['slots']['count']+1))
    blocked = ', '.join(NON_PUBLIC+n['block'])
    allowed = ['ct state established,related counter accept']
    if n['dns']:
        allowed.append(f"ip daddr {{ {', '.join(n['dns'])} }} meta l4proto {{ tcp, udp }} th dport 53 counter accept")
    if n['tcp_ports']:
        allowed.append(f"tcp dport {{ {', '.join(map(str, n['tcp_ports']))} }} counter accept")
    if n['ntp']:
        allowed.append('udp dport 123 counter accept')
    forbidden = ''.join(f'        oifname "{i}" counter jump deny comment "forbidden interface {i}"\n'
                        for i in n['forbidden_interfaces'])
    accepts = ''.join(f'        {line}\n' for line in allowed)
    # Every deny rule has a comment: `burner status --network` shows its counter by name.
    return f'''# Generated by burner from {CONFIG}. Do not edit: `burner firewall` rewrites it.
# Only affects the {p}* bridges; Docker, libvirt and Tailscale tables are left alone.
table inet burner
delete table inet burner
table inet burner {{
    set non_public {{
        type ipv4_addr
        flags interval
        auto-merge
        elements = {{ {blocked} }}
    }}
    set wan {{
        type ipv4_addr
        flags interval
    }}
    set origin {{
        type ifname . ipv4_addr
        elements = {{ {origin} }}
    }}

    chain deny {{
        limit rate 6/minute burst 10 packets log prefix "BURNER DROP " level warn
        drop
    }}

    chain host_input {{
        type filter hook input priority -20; policy accept;
        # The host talks to guests through the QEMU agent, never over the network.
        iifname "{p}*" counter jump deny comment "guest to host"
    }}

    chain from_vm {{
        meta nfproto ipv6 counter jump deny comment "ipv6"
        iifname . ip saddr != @origin counter jump deny comment "spoofed source"
        ip daddr @non_public counter jump deny comment "private destination"
        ip daddr @wan counter jump deny comment "own public ip"
{forbidden}        ct state invalid counter jump deny comment "invalid state"
{accepts}        counter jump deny comment "port or protocol not allowed"
    }}

    chain forward {{
        type filter hook forward priority -20; policy accept;
        iifname "{p}*" jump from_vm
        oifname "{p}*" ct state established,related counter accept
        oifname "{p}*" counter jump deny comment "inbound to guest"
    }}
}}
'''


def normalize(value):
    """Canonical `nft -j`: no handles or counters, rule order preserved.

    The order of chains and sets and of set elements changes after a reboot without
    changing semantics; the order of rules inside a chain does not.
    """
    if isinstance(value, dict):
        if 'nftables' in value:
            declarations, rules = [], {}
            for entry in value['nftables']:
                if 'metainfo' in entry:
                    continue
                normal = normalize(entry)
                if 'rule' in normal:
                    r = normal['rule']
                    rules.setdefault((r.get('family', ''), r.get('table', ''), r.get('chain', '')), []).append(normal)
                else:
                    declarations.append(normal)

            def identity(entry):
                kind = next(iter(entry))
                data = entry[kind]
                return (kind, data.get('family', ''), data.get('table', ''), data.get('name', ''))
            ordered = sorted(declarations, key=identity)
            for key in sorted(rules):
                ordered.extend(rules[key])
            return {'nftables': ordered}
        if isinstance(value.get('set'), dict):
            s = normalize(value['set'])
            if 'elem' in s:
                s['elem'] = sorted(s['elem'], key=lambda e: json.dumps(e, sort_keys=True))
            return {'set': s}
        if isinstance(value.get('set'), list):
            return {'set': sorted((normalize(e) for e in value['set']), key=lambda e: json.dumps(e, sort_keys=True))}
        return {k: ({} if k == 'counter' else normalize(v)) for k, v in value.items()
                if k not in ('metainfo', 'handle')}
    if isinstance(value, list):
        return [normalize(v) for v in value if not (isinstance(v, dict) and 'metainfo' in v)]
    return value


def without_wan(ruleset):
    """The observed WAN changes on its own; it is not part of the policy to compare."""
    for entry in ruleset.get('nftables', []):
        if entry.get('set', {}).get('name') == 'wan':
            entry['set'].pop('elem', None)
    return ruleset


def nft_table():
    return json.loads(run('nft', '-j', 'list', 'table', 'inet', 'burner'))


def current_policy():
    return normalize(without_wan(nft_table()))


def drop_counters(ruleset):
    counters = {}
    for entry in ruleset.get('nftables', []):
        rule = entry.get('rule')
        if rule and rule.get('comment'):
            packets = next((e['counter']['packets'] for e in rule.get('expr', []) if 'counter' in e), 0)
            counters[rule['comment']] = counters.get(rule['comment'], 0)+packets
    return counters


def firewall(cfg):
    FIREWALL.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    FIREWALL.write_text(nft_rules(cfg))
    run('nft', '-c', '-f', str(FIREWALL))
    run('nft', '-f', str(FIREWALL))
    write(STATE/'policy.json', current_policy())
    event('filter-loaded', file=str(FIREWALL))
    try:
        wan(cfg)
    except Exception as error:
        event('wan-pending', detail=str(error))


def wan(cfg):
    """Blocks your own public IP: otherwise the guest reaches the router through NAT loopback."""
    observers = cfg['network']['wan_observers']
    if not observers:
        return None
    interface = json.loads(run('ip', '-j', '-4', 'route', 'show', 'default'))[0]['dev']
    seen = set()
    for url in observers:
        ip = ipaddress.IPv4Address(run('curl', '-4', '--interface', interface, '--fail', '--silent',
                                       '--max-time', '6', url, timeout=10))
        if not ip.is_global:
            raise RuntimeError('The observed WAN is not public; no VM will start.')
        seen.add(str(ip))
    if len(seen) != 1:
        raise RuntimeError('WAN observers disagree; no VM will start.')
    ip = seen.pop()
    run('nft', '-f', '-', stdin=f'flush set inet burner wan\nadd element inet burner wan {{ {ip} }}\n')
    write(STATE/'wan.json', {'ip': ip, 'checked': time.time()})
    return ip


NWFILTER = '''<filter name='burner-l2' chain='root'>
  <rule action='drop' direction='inout' priority='-1000'>
    <mac protocolid='ipv6'/>
  </rule>
  <filterref filter='no-mac-spoofing'/>
  <filterref filter='no-arp-spoofing'/>
  <filterref filter='no-ip-spoofing'/>
  <rule action='accept' direction='inout' priority='900'><mac protocolid='ipv4'/></rule>
  <rule action='accept' direction='inout' priority='900'><mac protocolid='arp'/></rule>
  <rule action='drop' direction='inout' priority='1000'/>
</filter>
'''


def net_xml(cfg, i):
    n = net(cfg, i)
    return (f"<network><name>{n['name']}</name><forward mode='nat'/>"
            f"<bridge name='{n['bridge']}' stp='off' delay='0'/><dns enable='no'/>"
            f"<ip address='{n['host']}' netmask='255.255.255.0'/></network>")


def check_isolation(cfg, i):
    if current_policy() != read(STATE/'policy.json'):
        raise RuntimeError('The loaded filter does not match the installed one: no VM will start.')
    wan(cfg)
    f = ET.fromstring(virsh('nwfilter-dumpxml', 'burner-l2'))
    if not any(r.get('action') == 'drop' and r.find('mac') is not None
               and r.find('mac').get('protocolid') == 'ipv6' for r in f.findall('rule')):
        raise RuntimeError('The libvirt IPv6 barrier is missing.')
    n = net(cfg, i)
    x = ET.fromstring(virsh('net-dumpxml', n['name']))
    if (x.find('bridge').get('name') != n['bridge'] or x.find('forward').get('mode') != 'nat'
            or x.find('ip').get('address') != n['host'] or x.find('ip/dhcp') is not None):
        raise RuntimeError(f"Network {n['name']} changed; the VM will not start.")


# ---------------------------------------------------------------- VM definition

def domain_xml(cfg, name, uid, i, disk, cache, seed):
    vm, n = cfg['vm'], net(cfg, i)
    kib = vm['memory_gib']*1048576
    d = ET.Element('domain', type='kvm')

    def sub(parent, tag, text=None, **attrs):
        e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
        e.text = text
        return e
    sub(d, 'name', name)
    sub(d, 'uuid', str(uid))
    sub(d, 'memory', str(kib), unit='KiB')
    sub(d, 'vcpu', str(vm['vcpus']))
    tune = sub(d, 'cputune')
    sub(tune, 'global_period', '100000')
    sub(tune, 'global_quota', str(int(vm['cpu_quota']*100000)))
    mem = sub(d, 'memtune')
    sub(mem, 'hard_limit', str(kib+2097152), unit='KiB')  # memory + 2 GiB for QEMU itself
    sub(mem, 'swap_hard_limit', str(kib+2097152), unit='KiB')
    os_ = sub(d, 'os')
    sub(os_, 'type', 'hvm', arch='x86_64', machine='q35')
    sub(os_, 'boot', dev='hd')
    features = sub(d, 'features')
    sub(features, 'acpi')
    sub(features, 'apic')
    sub(d, 'cpu', mode='host-passthrough')
    sub(d, 'clock', offset='utc')
    sub(d, 'on_poweroff', 'destroy')
    sub(d, 'on_reboot', 'restart')
    sub(d, 'on_crash', 'destroy')
    dev = sub(d, 'devices')
    for path, target, serial in ((disk, 'vda', None), (cache, 'vdb', 'burner-cache')):
        x = sub(dev, 'disk', type='file', device='disk')
        # unsafe ignores guest flushes: disks are thrown away, or promoted after a clean shutdown.
        sub(x, 'driver', name='qemu', type='qcow2', cache='unsafe', discard='unmap')
        sub(x, 'source', file=path)
        sub(x, 'target', dev=target, bus='virtio')
        if serial:
            sub(x, 'serial', serial)
    cd = sub(dev, 'disk', type='file', device='cdrom')
    sub(cd, 'driver', name='qemu', type='raw')
    sub(cd, 'source', file=seed)
    sub(cd, 'target', dev='sda', bus='sata')
    sub(cd, 'readonly')
    nic = sub(dev, 'interface', type='network')
    sub(nic, 'source', network=n['name'])
    sub(nic, 'mac', address=n['mac'])
    sub(nic, 'model', type='virtio')
    ref = sub(nic, 'filterref', filter='burner-l2')
    sub(ref, 'parameter', name='IP', value=n['guest'])
    channel = sub(dev, 'channel', type='unix')
    sub(channel, 'target', type='virtio', name='org.qemu.guest_agent.0')
    sub(dev, 'serial', type='pty')
    rng = sub(dev, 'rng', model='virtio')
    sub(rng, 'backend', '/dev/urandom', model='random')
    sub(dev, 'memballoon', model='none')
    return ET.tostring(d, encoding='unicode')


def guest_network(cfg, i):
    n = net(cfg, i)
    return {'version': 2, 'ethernets': {'ci': {
        'match': {'macaddress': n['mac']}, 'set-name': 'ci0', 'dhcp4': False, 'dhcp6': False,
        'accept-ra': False, 'addresses': [n['guest']+'/24'],
        'routes': [{'to': 'default', 'via': n['host']}],
        'nameservers': {'addresses': cfg['network']['dns']}}}}


def seed(folder, cfg, i, uid, user_data):
    # JSON is valid YAML: no escaping issues.
    (folder/'user-data').write_text('#cloud-config\n'+json.dumps(user_data))
    (folder/'meta-data').write_text(json.dumps({'instance-id': str(uid), 'local-hostname': 'burner'}))
    (folder/'network-config').write_text(json.dumps(guest_network(cfg, i)))
    iso = folder/'seed.iso'
    run('cloud-localds', '--network-config', str(folder/'network-config'), str(iso),
        str(folder/'user-data'), str(folder/'meta-data'))
    for f in folder.iterdir():
        if f.suffix != '.qcow2':
            kvm_owned(f, 0o640)
    return iso


SLOT_USER_DATA = {'hostname': 'burner', 'disable_root': True, 'ssh_pwauth': False, 'users': []}

# Guest files. They travel inside the cloud-init seed of the base, never over the network.
PREPARE = r'''#!/bin/bash
# Runs once, as root, while building the base. Ends by writing the success marker.
set -euxo pipefail
set -a; . /usr/local/lib/burner/base.env; set +a
export DEBIAN_FRONTEND=noninteractive
systemctl start qemu-guest-agent || true
# Faster boot and nothing competing with the job: the base is replaced, never updated.
systemctl mask apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service man-db.timer \
  motd-news.timer fwupd-refresh.timer ssh.service ssh.socket || true
apt-get purge -y snapd || true
useradd --create-home --shell /bin/bash --user-group runner
systemctl daemon-reload
systemctl enable --now burner-cache.service
install -d -m 0755 /etc/apt/keyrings
curl -fsSL --proto '=https' https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
. /etc/os-release
printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
  "$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.sources
apt-get update
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin $PACKAGES
usermod -aG docker runner
install -d -o runner -g runner -m 0700 /home/runner/actions-runner
curl -fsSL --proto '=https' -o /tmp/runner.tar.gz "$RUNNER_URL"
echo "$RUNNER_SHA256  /tmp/runner.tar.gz" | sha256sum -c -
tar -C /home/runner/actions-runner -xzf /tmp/runner.tar.gz
rm /tmp/runner.tar.gz
/home/runner/actions-runner/bin/installdependencies.sh
chown -R runner:runner /home/runner/actions-runner
if [ -x /usr/local/lib/burner/provision.sh ]; then /usr/local/lib/burner/provision.sh; fi
if runuser -u runner -- sudo -n true 2>/dev/null; then echo 'runner must not have sudo' >&2; exit 1; fi
runuser -u runner -- docker run --rm hello-world
apt-get clean
touch /var/lib/burner-base-ok
'''

CACHE_SH = r'''#!/bin/sh
# Mounts the repo cache disk and puts Docker, containerd and the runner caches on it.
set -eu
dev=/dev/disk/by-id/virtio-burner-cache
for _ in $(seq 40); do [ -e "$dev" ] && break; sleep 0.25; done
[ -e "$dev" ] || { echo 'The cache disk is missing' >&2; exit 1; }
blkid "$dev" >/dev/null 2>&1 || mkfs.ext4 -q -m 0 -L burner-cache "$dev"
mkdir -p /cache /var/lib/docker /var/lib/containerd
mount -o noatime,discard "$dev" /cache
mkdir -p /cache/docker /cache/containerd /cache/runner
mount --bind /cache/docker /var/lib/docker
mount --bind /cache/containerd /var/lib/containerd
chown runner:runner /cache/runner
'''

CACHE_UNIT = '''[Unit]
Description=burner cache disk
After=local-fs.target
Before=containerd.service docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/burner/cache.sh

[Install]
WantedBy=multi-user.target
'''

NEEDS_CACHE = '[Unit]\nRequires=burner-cache.service\nAfter=burner-cache.service\n'

REGISTER = r'''#!/usr/bin/env python3
"""Inside a clean guest: registers an ephemeral runner and runs it. Data and token via stdin."""
import json, os, pwd, subprocess, sys

data = json.load(sys.stdin)
user = pwd.getpwnam('runner')
folder = os.path.join(user.pw_dir, 'actions-runner')
os.initgroups('runner', user.pw_gid)
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.environ.update(HOME=user.pw_dir, USER='runner', LOGNAME='runner')
os.chdir(folder)
for value in [data['work_dir'], *data['env'].values()]:
    if value.startswith('/cache/runner/'):
        os.makedirs(value, exist_ok=True)
env = dict(os.environ, ACTIONS_RUNNER_INPUT_TOKEN=data.pop('token'))
subprocess.run(['./config.sh', '--unattended', '--ephemeral', '--url', data['url'],
                '--name', data['name'], '--labels', data['label'], '--work', data['work_dir']],
               env=env, check=True, timeout=120)
del env['ACTIONS_RUNNER_INPUT_TOKEN']
# The runner reads .env at startup; config.sh rewrites it, so this goes after.
with open('.env', 'a') as f:
    f.writelines(f'{k}={v}\n' for k, v in data['env'].items())
os.execve('./run.sh', ['./run.sh'], env)
'''


def base_user_data(cfg, runner):
    c = cfg['cache']
    daemon = {'log-driver': 'json-file', 'log-opts': {'max-size': '10m', 'max-file': '3'},
              'default-address-pools': [{'base': '172.28.0.0/16', 'size': 24}],
              'builder': {'gc': {'enabled': True, 'defaultKeepStorage': f"{c['buildkit_gib']}GB"}}}
    env = (f"RUNNER_URL={runner['url']}\nRUNNER_SHA256={runner['sha256']}\n"
           f"PACKAGES='{' '.join(cfg['vm']['packages'])}'\n")
    files = [('/usr/local/lib/burner/base.env', env, '0644'),
             ('/usr/local/lib/burner/prepare.sh', PREPARE, '0755'),
             ('/usr/local/lib/burner/cache.sh', CACHE_SH, '0755'),
             ('/usr/local/lib/burner/register.py', REGISTER, '0755'),
             ('/etc/systemd/system/burner-cache.service', CACHE_UNIT, '0644'),
             ('/etc/systemd/system/docker.service.d/burner.conf', NEEDS_CACHE, '0644'),
             ('/etc/systemd/system/containerd.service.d/burner.conf', NEEDS_CACHE, '0644'),
             ('/etc/docker/daemon.json', json.dumps(daemon), '0644'),
             ('/etc/cloud/cloud.cfg.d/99-burner.cfg', 'datasource_list: [NoCloud, None]\n', '0644')]
    if cfg['vm']['provision']:
        files.append(('/usr/local/lib/burner/provision.sh', Path(cfg['vm']['provision']).read_text(), '0755'))
    return {**SLOT_USER_DATA, 'package_update': True, 'package_upgrade': True,
            'packages': ['qemu-guest-agent'],
            'write_files': [{'path': p, 'permissions': m, 'encoding': 'b64',
                             'content': base64.b64encode(t.encode()).decode()} for p, t, m in files],
            'runcmd': [['/usr/local/lib/burner/prepare.sh']]}


# ---------------------------------------------------------------- GitHub

class GitHubError(RuntimeError):
    def __init__(self, code):
        super().__init__(f'GitHub HTTP {code}')
        self.code = code


class GitHub:
    """GitHub App installation token, scoped to one repo and kept in memory only."""

    def __init__(self, rc):
        self.rc = rc
        key = Path(rc['key'])
        info = key.stat()
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise RuntimeError(f'{key} must be owned by root with mode 0600.')
        self.token, self.expires = None, 0

    @staticmethod
    def request(path, token=None, data=None, method=None):
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'burner'}
        if token:
            headers['Authorization'] = 'Bearer '+token
        url = path if path.startswith('https://') else 'https://api.github.com'+path
        req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                body = response.read(16*1024*1024)
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            # No headers or body: they may carry the token.
            raise GitHubError(error.code) from None

    def authenticate(self):
        if self.token and time.time() < self.expires:
            return self.token

        def b64(value):
            return base64.urlsafe_b64encode(value).rstrip(b'=')
        now = int(time.time())
        message = b64(b'{"alg":"RS256","typ":"JWT"}')+b'.'+b64(json.dumps(
            {'iat': now-60, 'exp': now+540, 'iss': str(self.rc['app_id'])}).encode())
        signature = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', self.rc['key']],
                                   input=message, capture_output=True, check=True, timeout=10).stdout
        jwt = (message+b'.'+b64(signature)).decode()
        self.token = self.request(f"/app/installations/{self.rc['installation_id']}/access_tokens", jwt,
                                  {'repositories': [self.rc['name'].split('/')[1]]})['token']
        self.expires = time.time()+3000  # GitHub issues them for one hour
        return self.token

    def api(self, path, data=None, method=None):
        return self.request(f"/repos/{self.rc['name']}{path}", self.authenticate(), data, method)

    def pages(self, path, field):
        sep = '&' if '?' in path else '?'
        for page in range(1, 51):
            values = self.api(f'{path}{sep}per_page=100&page={page}')[field]
            yield from values
            if len(values) < 100:
                return
        raise RuntimeError('The queue exceeds the pagination limit.')

    def runner(self, name):
        return next((r for r in self.api(f'/actions/runners?name={name}&per_page=100')['runners']
                     if r['name'] == name), None)


def admissible(run_, job, rc):
    labels = {e.lower() for e in job.get('labels', [])}
    return ((run_.get('head_repository') or {}).get('full_name') == rc['name']  # never forks
            and (not rc['workflows'] or run_.get('path') in rc['workflows'])
            and job.get('status') == 'queued'
            and rc['label'] in labels and labels <= GITHUB_LABELS | {rc['label']})


def queued(gh, rc):
    total = 0
    for status in ('queued', 'in_progress'):
        for run_ in gh.pages(f'/actions/runs?status={status}', 'workflow_runs'):
            if (run_.get('head_repository') or {}).get('full_name') != rc['name']:
                continue
            total += sum(admissible(run_, job, rc)
                         for job in gh.pages(f"/actions/runs/{run_['id']}/jobs?filter=latest", 'jobs'))
    return total


def trusted(run_, job, rc):
    """Only a successful job of code already merged into a trusted branch writes the cache."""
    return (run_.get('event') in TRUSTED_EVENTS
            and run_.get('head_branch') in rc['trusted_branches']
            and (run_.get('head_repository') or {}).get('full_name') == rc['name']
            and (not rc['workflows'] or run_.get('path') in rc['workflows'])
            and job.get('conclusion') == 'success')


def job_of(gh, name, since):
    """Which job ran on this runner, according to GitHub: the guest's word is not trusted.

    Labels do not tie a job to a runner: any queued job with those labels may take it.
    """
    floor = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(since-60))
    for run_ in gh.api('/actions/runs?per_page=30')['workflow_runs']:
        if run_.get('updated_at', '') < floor:
            continue
        for job in gh.pages(f"/actions/runs/{run_['id']}/jobs?filter=latest", 'jobs'):
            if job.get('runner_name') == name:
                return run_, job
    return None


def heartbeat(cfg):
    """A repo variable holding the time: the workflow picks burner or ubuntu-latest by its age."""
    for rc in cfg['repos']:
        if not rc['heartbeat']:
            continue
        data = {'name': rc['heartbeat'], 'value': str(int(time.time()))}
        gh = GitHub(rc)
        try:
            gh.api(f"/actions/variables/{rc['heartbeat']}", data, 'PATCH')
        except GitHubError as error:
            if error.code != 404:
                raise
            gh.api('/actions/variables', data, 'POST')


def official_runner():
    release = GitHub.request('/repos/actions/runner/releases/latest')
    assets = [a for a in release['assets']
              if re.fullmatch(r'actions-runner-linux-x64-[0-9.]+\.tar\.gz', a['name'])]
    if len(assets) != 1:
        raise RuntimeError('No single official Linux x64 runner in the latest release.')
    a = assets[0]
    digest, url = a.get('digest', ''), a['browser_download_url']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or not url.startswith(
            'https://github.com/actions/runner/releases/download/'):
        raise RuntimeError('The runner lacks a verifiable official origin and hash.')
    return {'url': url, 'sha256': digest[7:], 'version': release['tag_name']}


# ---------------------------------------------------------------- slot lifecycle

def slot_file(i):
    return STATE/f'slot-{i}.json'


def domain(i):
    return f'burner-slot-{i}'


def create(cfg, i, state, base):
    """Boots slot i from the base and the repo cache, on overlays that are thrown away."""
    check_isolation(cfg, i)
    dom, n = domain(i), net(cfg, i)
    if exists(dom):
        raise RuntimeError(f'{dom} already exists; a job is never replaced.')
    if n['name'] not in virsh('net-list', '--name').splitlines():
        virsh('net-start', n['name'])
    foreign = [p.name for p in Path(f"/sys/class/net/{n['bridge']}/brif").iterdir() if not p.name.startswith('vnet')]
    if foreign:
        raise RuntimeError(f"Non-KVM ports on {n['bridge']}: {foreign}")
    uid = uuid.UUID(state['id'])
    folder = kvm_dir(DISKS/'slots'/uid.hex)
    cache_base = current(DISKS/'caches'/slug(state['repo'])/'current') if state.get('repo') else None
    state.update(phase='creating', base=str(base), cache_base=str(cache_base) if cache_base else '')
    write(slot_file(i), state)
    overlay(base, folder/'disk.qcow2', cfg['vm']['disk_gib'])
    overlay(cache_base, folder/'cache.qcow2', cfg['cache']['gib'])
    iso = seed(folder, cfg, i, uid, SLOT_USER_DATA)
    xml = folder/'domain.xml'
    xml.write_text(domain_xml(cfg, dom, uid, i, folder/'disk.qcow2', folder/'cache.qcow2', iso))
    virsh('define', str(xml))
    virsh('start', dom)
    live = ET.fromstring(virsh('dumpxml', dom))
    refs = [nic.find('filterref') for nic in live.findall('./devices/interface')]
    if (len(refs) != 1 or refs[0] is None or refs[0].get('filter') != 'burner-l2'
            or live.findall('./devices/filesystem') or live.findall('./devices/hostdev')):
        raise RuntimeError('The VM started without the expected isolation.')
    wait_for_agent(dom, 180)
    guest(dom, ['/usr/bin/cloud-init', 'status', '--wait'], 300, codes=(0, 2))
    guest(dom, ['/usr/bin/systemctl', 'is-active', '--quiet', 'burner-cache.service', 'docker.service'])
    guest(dom, ['/usr/bin/curl', '--fail', '--silent', '--head', '--max-time', '15', 'https://github.com'])
    state['phase'] = 'ready'
    write(slot_file(i), state)


def discard(i, state):
    dom = domain(i)
    if exists(dom):
        if state and virsh('domuuid', dom) != state['id']:
            raise RuntimeError(f'{dom} has another identity: leaving it alone.')
        if virsh('domstate', dom) != 'shut off':
            virsh('destroy', dom)
        virsh('undefine', dom)
    if state:
        shutil.rmtree(DISKS/'slots'/uuid.UUID(state['id']).hex, ignore_errors=True)


def clean(cfg, i, state):
    """Destroys the VM and removes its runner. If GitHub fails, the state stays for a retry."""
    discard(i, state)
    if state.get('runner'):
        gh = GitHub(repo(cfg, state['repo']))
        r = gh.runner(state['runner'])
        if r:
            gh.api(f"/actions/runners/{int(r['id'])}", method='DELETE')
    slot_file(i).unlink(missing_ok=True)
    event('discarded', slot=i, id=state.get('id'))


def watch_runner(cfg, gh, name, i, state):
    """Until the ephemeral runner finishes its job, according to the GitHub API."""
    start, existed, busy = time.monotonic(), False, False
    while True:
        r = gh.runner(name)
        existed = existed or r is not None
        if r and r['busy']:
            if not busy:
                busy = True
                state['phase'] = 'busy'
                write(slot_file(i), state)
        elif busy or (existed and r is None):
            return 'finished'
        elif time.monotonic()-start > cfg['slots']['idle_timeout_s']:
            return 'idle'
        time.sleep(10)


def promote(cfg, i, state):
    """The cache left by a trusted job becomes the repo cache; pull requests only read it."""
    dom = domain(i)
    state['phase'] = 'promoting'
    write(slot_file(i), state)
    with contextlib.suppress(Exception):
        qga(dom, {'execute': 'guest-fstrim'}, timeout=300)
    if not shut_down(dom):
        event('promotion-skipped', slot=i, reason='no clean shutdown')
        return
    folder = DISKS/'caches'/slug(state['repo'])
    new = seal_image(DISKS/'slots'/uuid.UUID(state['id']).hex/'cache.qcow2', folder, 'cache')
    if new.stat().st_size > cfg['cache']['max_gib']*1024**3:
        new.unlink()
        (folder/'current').unlink(missing_ok=True)
        event('cache-reset', repo=state['repo'], reason='exceeded cache.max_gib')
    else:
        point(folder/'current', new)
        event('cache-promoted', repo=state['repo'], file=new.name, bytes=new.stat().st_size)
    prune(folder, 'cache')


def slot(cfg, i):
    if not (STATE/'active').exists():
        return
    with lock(f'slot-{i}', wait=False) as mine:
        if not mine:
            return
        if state := read(slot_file(i)):
            event('recovering', slot=i)
            clean(cfg, i, state)
        base = current(DISKS/'bases'/'current')
        if not base:
            event('no-base')
            return
        with lock('admission'):
            others = [read(f) for f in STATE.glob('slot-*.json')]
            for rc in cfg['repos']:
                gh = GitHub(rc)
                waiting = sum(o.get('repo') == rc['name'] and o.get('phase') in WAITING_PHASES for o in others)
                if queued(gh, rc) > waiting:
                    state = {'id': str(uuid.uuid4()), 'repo': rc['name'], 'phase': 'reserved',
                             'created': time.time(), 'expires': time.time()+cfg['slots']['timeout_min']*60,
                             'boot_id': boot_id()}
                    write(slot_file(i), state)
                    break
            else:
                return
        signal.alarm(cfg['slots']['timeout_min']*60)
        try:
            create(cfg, i, state, base)
            name = 'burner-'+uuid.UUID(state['id']).hex
            token = gh.api('/actions/runners/registration-token', {})['token']
            state.update(phase='registered', runner=name)
            write(slot_file(i), state)
            c = cfg['cache']
            guest(domain(i), ['/usr/bin/python3', '/usr/local/lib/burner/register.py'], wait=False,
                  stdin=json.dumps({'token': token, 'name': name, 'url': f"https://github.com/{rc['name']}",
                                    'label': rc['label'], 'work_dir': c['work_dir'], 'env': c['env']}))
            del token
            event('waiting-for-job', slot=i, runner=name, repo=rc['name'])
            end = watch_runner(cfg, gh, name, i, state)
            found = job_of(gh, name, state['created']) if end == 'finished' else None
            run_, job = found or ({}, {})
            event('job', slot=i, end=end, run=run_.get('id'), job=job.get('id'), trigger=run_.get('event'),
                  branch=run_.get('head_branch'), conclusion=job.get('conclusion'), name=job.get('name'))
            if found and trusted(run_, job, rc):
                promote(cfg, i, state)
        finally:
            signal.alarm(0)
            clean(cfg, i, read(slot_file(i)) or state)


# ---------------------------------------------------------------- base image

def download_ubuntu(cfg):
    series = cfg['vm']['ubuntu']
    url = f'https://cloud-images.ubuntu.com/{series}/current/'
    name = f'{series}-server-cloudimg-amd64.img'
    DOWNLOADS.mkdir(mode=0o700, parents=True, exist_ok=True)

    def fetch(source, target):
        run('curl', '-fsSL', '--proto', '=https', '--retry', '3', '-o', str(target), source, timeout=3600)
    for f in ('SHA256SUMS', 'SHA256SUMS.gpg'):
        fetch(url+f, DOWNLOADS/f)
    run('gpgv', '--keyring', '/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg',
        str(DOWNLOADS/'SHA256SUMS.gpg'), str(DOWNLOADS/'SHA256SUMS'))
    expected = next(line.split()[0] for line in (DOWNLOADS/'SHA256SUMS').read_text().splitlines()
                    if line.endswith(' *'+name))
    image = DOWNLOADS/name
    if not image.exists() or sha256(image) != expected:
        fetch(url+name, image.with_suffix('.partial'))
        if sha256(image.with_suffix('.partial')) != expected:
            raise RuntimeError('The Ubuntu image does not match the signed SHA256SUMS.')
        image.with_suffix('.partial').replace(image)
    copy = kvm_dir(DISKS)/'ubuntu.img'
    shutil.copyfile(image, copy)
    kvm_owned(copy, 0o440)
    return copy


def build_base(cfg):
    with lock('base', wait=False) as mine:
        if not mine:
            raise RuntimeError('A base build is already running.')
        image = download_ubuntu(cfg)
        runner = official_runner()
        check_isolation(cfg, 0)
        dom, folder, uid = 'burner-base', DISKS/'build', uuid.uuid4()
        if exists(dom):
            with contextlib.suppress(Exception):
                virsh('destroy', dom)
            virsh('undefine', dom)
        shutil.rmtree(folder, ignore_errors=True)
        kvm_dir(folder)
        n = net(cfg, 0)
        if n['name'] not in virsh('net-list', '--name').splitlines():
            virsh('net-start', n['name'])
        write(STATE/'building.json', {'started': time.time(), 'runner': runner['version']}, 0o644)
        try:
            overlay(image, folder/'disk.qcow2', cfg['vm']['disk_gib'])
            overlay(None, folder/'cache.qcow2', cfg['cache']['gib'])
            iso = seed(folder, cfg, 0, uid, base_user_data(cfg, runner))
            (folder/'domain.xml').write_text(
                domain_xml(cfg, dom, uid, 0, folder/'disk.qcow2', folder/'cache.qcow2', iso))
            virsh('define', str(folder/'domain.xml'))
            virsh('start', dom)
            event('base-building', runner=runner['version'])
            wait_for_agent(dom, 1800)
            guest(dom, ['/usr/bin/cloud-init', 'status', '--wait'], 3600, codes=(0, 1, 2))
            try:
                guest(dom, ['/usr/bin/test', '-e', '/var/lib/burner-base-ok'])
            except RuntimeError:
                print(printable(guest(dom, ['/usr/bin/tail', '-n', '80', '/var/log/cloud-init-output.log'])),
                      flush=True)
                raise RuntimeError('Preparing the base failed; the end of cloud-init is above.') from None
            guest(dom, ['/bin/sh', '-c', 'rm -f /var/lib/burner-base-ok /etc/ssh/ssh_host_* && '
                                         'cloud-init clean --logs --machine-id --seed'])
            with contextlib.suppress(Exception):
                qga(dom, {'execute': 'guest-fstrim'}, timeout=300)
            if not shut_down(dom, 180):
                raise RuntimeError('The build VM did not shut down; a disk in use is never copied.')
            candidate = seal_image(folder/'disk.qcow2', DISKS/'bases', 'base')
        finally:
            with contextlib.suppress(Exception):
                if virsh('domstate', dom) != 'shut off':
                    virsh('destroy', dom)
            with contextlib.suppress(Exception):
                virsh('undefine', dom)
            shutil.rmtree(folder, ignore_errors=True)
            image.unlink(missing_ok=True)
            (STATE/'building.json').unlink(missing_ok=True)
        try:
            selftest(cfg, candidate)
        except BaseException:
            candidate.unlink()
            raise
        point(DISKS/'bases'/'current', candidate)
        prune(DISKS/'bases', 'base')
        event('base-ready', file=candidate.name, runner=runner['version'])


NETWORK_PROBE = r'''
import json, socket, sys
targets = json.loads(sys.argv[1])
def tcp(h, p):
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((h, p)); return 'connects'
    except socket.timeout:
        return 'times out'
    except ConnectionRefusedError:
        return 'refused'
    except OSError as e:
        return 'error %s' % e.errno
    finally:
        s.close()
failures = []
if tcp('github.com', 443) != 'connects':
    failures.append('github.com:443 does not connect')
for h, p in targets:
    r = tcp(h, p)
    # "refused" is a failure too: the packet got there. Only a timeout counts.
    if r != 'times out':
        failures.append('%s:%s %s' % (h, p, r))
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.settimeout(3)
# A query for github.com to a resolver that is not allowed.
u.sendto(bytes.fromhex('123401000001000000000000' '06676974687562' '03636f6d' '00' '00010001'), ('9.9.9.9', 53))
try:
    u.recv(512); failures.append('DNS to 9.9.9.9 answers')
except socket.timeout:
    pass
print(json.dumps({'blocks_tested': len(targets)+1, 'failures': failures}))
sys.exit(1 if failures else 0)
'''


def blocked_targets(cfg, i):
    n, other = net(cfg, i), net(cfg, 1 if i == 0 else 0)
    targets = [(n['host'], p) for p in (22, 53, 80, 443, 16509)]
    targets += [(other['host'], 22), (other['guest'], 22), ('10.0.0.1', 80), ('172.16.0.1', 80),
                ('192.168.0.1', 80), ('192.168.1.1', 443), ('100.100.100.100', 53),
                ('169.254.169.254', 80), ('1.1.1.1', 22)]
    targets += [(str(ipaddress.ip_network(b)[1]), 443) for b in cfg['network']['block']]
    if ip := read(STATE/'wan.json').get('ip'):
        targets += [(ip, 80), (ip, 443)]
    return targets


def selftest(cfg, base=None):
    """Two copies on the maintenance slot: new identity and disk, closed network, untouched base."""
    base = base or current(DISKS/'bases'/'current')
    if not base:
        raise RuntimeError('No base yet: run `burner base`.')
    with lock('slot-0', wait=False) as mine:
        if not mine or read(slot_file(0)):
            raise RuntimeError('The maintenance slot is busy.')
        fingerprint = sha256(base)
        ids = []
        for copy in (1, 2):
            state = {'id': str(uuid.uuid4()), 'repo': '', 'phase': 'reserved', 'created': time.time(),
                     'expires': time.time()+1800, 'boot_id': boot_id()}
            write(slot_file(0), state)
            try:
                create(cfg, 0, state, base)
                state['phase'] = 'testing'
                write(slot_file(0), state)
                dom = domain(0)
                guest(dom, ['/bin/sh', '-c', 'test ! -e /var/tmp/burner-mark && touch /var/tmp/burner-mark'])
                ids.append(guest(dom, ['/usr/bin/cat', '/etc/machine-id']).strip())
                guest(dom, ['/usr/bin/runuser', '-u', 'runner', '--', '/usr/bin/docker', 'info'], 60)
                print(printable(guest(dom, ['/usr/bin/python3', '-c', NETWORK_PROBE,
                                            json.dumps(blocked_targets(cfg, 0))], 300)), flush=True)
                event('selftest-copy-ok', copy=copy)
            finally:
                clean(cfg, 0, read(slot_file(0)) or state)
        if len(set(ids)) != 2:
            raise RuntimeError('The copies repeated the machine-id.')
        if sha256(base) != fingerprint:
            raise RuntimeError('The base changed during the test.')
        write(STATE/'selftest.json', {'time': time.time(), 'base': base.name, 'sha256': fingerprint}, 0o644)
        event('selftest-ok', base=base.name)


# ---------------------------------------------------------------- operation

def install(cfg):
    for program in ('virsh', 'nft', 'qemu-img', 'cloud-localds', 'gpgv', 'curl', 'openssl'):
        if not shutil.which(program):
            raise RuntimeError(f'{program} is missing: run install.sh.')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    kvm_dir(DISKS)
    firewall(cfg)
    run('systemctl', 'enable', '--now', 'burner-firewall.service', 'burner-reaper.timer', 'burner-snapshot.service')
    tmp = STATE/'nwfilter.xml'
    tmp.write_text(NWFILTER)
    virsh('nwfilter-define', str(tmp))
    for i in range(cfg['slots']['count']+1):
        n = net(cfg, i)
        if n['name'] in virsh('net-list', '--all', '--name').splitlines():
            x = ET.fromstring(virsh('net-dumpxml', n['name']))
            if x.find('bridge').get('name') != n['bridge'] or x.find('ip').get('address') != n['host']:
                raise RuntimeError(f"Network {n['name']} exists with other settings: remove it by hand.")
            continue
        tmp.write_text(net_xml(cfg, i))
        virsh('net-define', str(tmp))
    tmp.unlink()
    event('installed', slots=cfg['slots']['count'], next='burner check && burner base')


def check(cfg):
    for rc in cfg['repos']:
        gh = GitHub(rc)
        repos = gh.request('/installation/repositories', gh.authenticate())
        if [r['full_name'] for r in repos['repositories']] != [rc['name']]:
            raise RuntimeError(f"The token for {rc['name']} is not scoped to that repo alone.")
        gh.api('/actions/runners')
        gh.api('/actions/runs?per_page=1')
        event('github-ok', repo=rc['name'])
    heartbeat(cfg)


def slot_timers():
    out = run('systemctl', 'list-timers', '--all', '--plain', '--no-legend', 'burner-slot@*.timer')
    return [w for line in out.splitlines() for w in line.split()
            if w.startswith('burner-slot@') and w.endswith('.timer')]


def activate(cfg):
    if not current(DISKS/'bases'/'current'):
        raise RuntimeError('No tested base: run `burner base`.')
    for t in slot_timers():
        if int(t.split('@')[1].split('.')[0]) > cfg['slots']['count']:
            run('systemctl', 'disable', '--now', t)
    (STATE/'active').touch()
    run('systemctl', 'enable', '--now', 'burner-base.timer',
        *(f'burner-slot@{i}.timer' for i in range(1, cfg['slots']['count']+1)))
    event('active')


def pause(now):
    (STATE/'active').unlink(missing_ok=True)
    if timers := slot_timers():
        run('systemctl', 'disable', '--now', *timers)
    if now:
        # SIGTERM: each slot discards its VM and removes its runner before exiting.
        run('systemctl', 'stop', 'burner-slot@*.service', timeout=300)
    event('paused', running_jobs='cut' if now else 'finish on their own')


def reap(cfg):
    for f in STATE.glob('slot-*.json'):
        i = int(f.stem.split('-')[1])
        with lock(f'slot-{i}', wait=False) as mine:
            state = read(f)
            if not mine or not state:
                continue
            # Every writer holds the slot lock: if it is free, whatever is left belongs to a dead process.
            try:
                clean(cfg, i, state)
            except Exception as error:
                event('cleanup-pending', slot=i, detail=str(error)[-500:])
    live = {uuid.UUID(read(f)['id']).hex for f in STATE.glob('slot-*.json') if read(f).get('id')}
    for folder in (DISKS/'slots').glob('*'):
        if folder.name not in live:
            shutil.rmtree(folder, ignore_errors=True)
    for dom in virsh('list', '--all', '--name').splitlines():
        i = dom.removeprefix('burner-slot-')
        if i != dom and not slot_file(int(i)).exists():
            with lock(f'slot-{i}', wait=False) as mine:
                if mine and not slot_file(int(i)).exists():
                    discard(int(i), None)
    try:
        if time.time()-read(STATE/'wan.json').get('checked', 0) > 300:
            wan(cfg)
    except Exception as error:
        # Without a verified WAN no VM starts: no heartbeat either.
        event('wan-failed', detail=str(error)[-500:])
        return
    if (STATE/'active').exists() and current(DISKS/'bases'/'current') and current_policy() == read(STATE/'policy.json'):
        heartbeat(cfg)


def cache_reset(cfg, name):
    repo(cfg, name)
    (DISKS/'caches'/slug(name)/'current').unlink(missing_ok=True)
    prune(DISKS/'caches'/slug(name), 'cache')
    event('cache-reset', repo=name, reason='manual')


def exec_in(slot_n, argv):
    """Debugging: runs a command in a slot VM through the agent. Output is sanitized."""
    if not argv:
        raise RuntimeError('Usage: burner exec SLOT -- command [args]')
    print(printable(guest(domain(slot_n), argv, 600, codes=None)), end='')


# ---------------------------------------------------------------- status snapshot (root) and views (anyone in the group)

def cgroup(dom):
    # libvirt scope: machine-qemu\x2d<id>\x2d<name>.scope, with '-' escaped.
    escaped = dom.replace('-', '\\x2d')
    found = list(Path('/sys/fs/cgroup/machine.slice').glob(f'machine-qemu\\x2d*\\x2d{escaped}.scope'))
    return found[0] if found else None


def read_int(path, key=None):
    """First number of a /sys file, or the value of `key` in a flat keyed file like cpu.stat."""
    try:
        text = Path(path).read_text()
        if key:
            return int(next(line.split()[1] for line in text.splitlines() if line.startswith(key+' ')))
        return int(text.split()[0])
    except (OSError, ValueError, IndexError, StopIteration):
        return 0


def sample(cfg):
    """Everything the monitor shows, collected as root, without secrets."""
    cpu = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    mem = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    disk = os.statvfs(DISKS if DISKS.exists() else '/')
    try:
        ruleset = nft_table()
        drops = drop_counters(ruleset)
        policy_ok = normalize(without_wan(ruleset)) == read(STATE/'policy.json')
    except Exception:
        policy_ok, drops = False, {}
    slots = {}
    for i in range(cfg['slots']['count']+1):
        n, state = net(cfg, i), read(slot_file(i))
        scope = cgroup(domain(i))
        ports = list(Path(f"/sys/class/net/{n['bridge']}/brif").glob('vnet*'))
        stats = ports[0]/'statistics' if ports else None
        slots[i] = {k: state.get(k) for k in ('phase', 'repo', 'runner', 'created', 'expires')}
        slots[i].update(running=scope is not None,
                        cpu_usec=read_int(scope/'cpu.stat', 'usage_usec') if scope else 0,
                        mem_bytes=read_int(scope/'memory.current') if scope else 0,
                        # Seen from the host: what the host receives on vnet is what the guest sends.
                        out_bytes=read_int(stats/'rx_bytes') if stats else 0,
                        in_bytes=read_int(stats/'tx_bytes') if stats else 0)
    caches = {}
    for rc in cfg['repos']:
        c = current(DISKS/'caches'/slug(rc['name'])/'current')
        caches[rc['name']] = {'file': c.name, 'bytes': c.stat().st_size, 'time': c.stat().st_mtime} if c else None
    base = current(DISKS/'bases'/'current')
    return {
        'time': time.time(), 'host': socket.gethostname(), 'active': (STATE/'active').exists(),
        'cpu_total': sum(cpu), 'cpu_idle': cpu[3]+cpu[4], 'threads': os.cpu_count(),
        'load': float(Path('/proc/loadavg').read_text().split()[0]),
        'mem_total': mem['MemTotal']*1024, 'mem_available': mem['MemAvailable']*1024,
        'disk_free': disk.f_bavail*disk.f_frsize,
        'uptime': float(Path('/proc/uptime').read_text().split()[0]),
        'policy_ok': policy_ok, 'drops': drops, 'wan': read(STATE/'wan.json'),
        'base': {'file': base.name, 'time': base.stat().st_mtime} if base else None,
        'building': read(STATE/'building.json'), 'selftest': read(STATE/'selftest.json'),
        'caches': caches, 'slots': slots,
    }


def with_rates(new, old):
    dt = new['time']-old.get('time', new['time'])
    if dt <= 0:
        return new
    total = new['cpu_total']-old['cpu_total']
    if total > 0:
        new['cpu_pct'] = round(100*(1-(new['cpu_idle']-old['cpu_idle'])/total), 1)
    for i, s in new['slots'].items():
        o = old['slots'].get(i, {})
        if s['running'] and o.get('running'):
            s['cpu_pct'] = round((s['cpu_usec']-o['cpu_usec'])/dt/1e4, 1)
            s['out_bps'] = max(0, (s['out_bytes']-o['out_bytes'])/dt)
            s['in_bps'] = max(0, (s['in_bytes']-o['in_bytes'])/dt)
    return new


def snapshot(cfg, loop):
    """Root writes /run/burner/status.json, readable by the burner group: the monitor never needs sudo."""
    RUN.mkdir(parents=True, exist_ok=True)
    gid = grp.getgrnam(GROUP).gr_gid
    os.chown(RUN, 0, gid)
    os.chmod(RUN, 0o750)
    old = {}
    while True:
        new = with_rates(sample(cfg), old)
        path = write(RUN/'status.json', new, 0o640)
        os.chown(path, 0, gid)
        old = new
        if not loop:
            return
        time.sleep(2)


class Paint:
    def __init__(self, on):
        self.on = on

    def __call__(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.on else str(text)


def human_bytes(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def ago(seconds):
    seconds = int(seconds)
    if seconds < 120:
        return f'{seconds}s'
    if seconds < 7200:
        return f'{seconds // 60}m'
    if seconds < 172800:
        return f'{seconds // 3600}h'
    return f'{seconds // 86400}d'


PHASE_COLORS = {'busy': '1;32', 'registered': '36', 'ready': '36', 'creating': '33', 'reserved': '33',
                'promoting': '35', 'testing': '35'}


def render(s, paint, network=False):
    now = time.time()
    age = now-s['time']
    lines = []
    head = paint(' BURNER ', '1;7')+f"  {s['host']}   " + \
        (paint('ACTIVE', '1;32') if s['active'] else paint('PAUSED', '1;33'))
    head += paint(f'   snapshot {ago(age)} old', '1;31' if age > 15 else '2')
    lines += [head, '']
    if network:
        lines.append(paint(' Filter ', '1')+(paint('matches installed policy', '32') if s['policy_ok']
                                              else paint('DOES NOT MATCH: no VM will start', '1;31')))
        wan = s.get('wan') or {}
        lines.append(' WAN    ' + (f"{wan['ip']} blocked, checked {ago(now-wan['checked'])} ago" if wan
                                    else paint('not verified', '33')))
        lines += ['', paint(' Dropped packets since the filter was loaded (includes self-tests)', '1')]
        for name, packets in sorted(s['drops'].items(), key=lambda kv: -kv[1]):
            lines.append(f'  {name:32} ' + paint(f'{packets:>10}', '1;31' if packets else '2'))
        lines += ['', paint(' Allowed: TCP 80/443, DNS to the configured resolvers, NTP.', '2'),
                  paint(' A green panel does not prove isolation: run `sudo burner selftest`.', '2')]
        return '\n'.join(lines)
    mem_used = s['mem_total']-s['mem_available']
    lines.append(' Host   ' + f"CPU {s.get('cpu_pct', 0):5.1f}% of {s['threads']} threads   load {s['load']:.2f}   "
                 f"RAM {human_bytes(mem_used)} / {human_bytes(s['mem_total'])}   "
                 f"disk free {human_bytes(s['disk_free'])}   up {ago(s['uptime'])}")
    base = s.get('base')
    test = s.get('selftest') or {}
    lines.append(' Base   ' + (f"{base['file']} ({ago(now-base['time'])} old)" if base else paint('none', '1;31'))
                 + (f"   self-test {ago(now-test['time'])} ago" if test else '')
                 + (paint(f"   building since {ago(now-s['building']['started'])}", '33') if s.get('building') else ''))
    lines.append(' Filter ' + (paint('ok', '32') if s['policy_ok'] else paint('MISMATCH', '1;31'))
                 + f"   dropped {sum(s['drops'].values())} packets")
    lines += ['', paint(f" {'SLOT':<5}{'PHASE':<12}{'REPO':<28}{'AGE':>6}{'CPU':>8}{'RAM':>11}{'OUT/s':>11}{'IN/s':>11}", '1')]
    for i, sl in sorted(s['slots'].items(), key=lambda kv: int(kv[0])):
        label = 'maint' if int(i) == 0 else str(i)
        phase = sl.get('phase') or ('running?' if sl['running'] else 'idle')
        if int(i) == 0 and phase == 'idle':
            continue
        row = (f" {label:<5}" + paint(f'{phase:<12}', PHASE_COLORS.get(phase, '2'))
               + f"{(sl.get('repo') or '-')[:27]:<28}"
               + f"{ago(now-sl['created']) if sl.get('created') else '-':>6}")
        if sl['running']:
            row += (f"{sl.get('cpu_pct', 0):>7.0f}%{human_bytes(sl['mem_bytes']):>11}"
                    f"{human_bytes(sl.get('out_bps', 0)):>11}{human_bytes(sl.get('in_bps', 0)):>11}")
        lines.append(row)
        if sl.get('expires') and sl['expires']-now < 300:
            lines.append(paint(f'       expires in {ago(max(0, sl["expires"]-now))}', '1;33'))
    lines += ['', paint(' CACHES', '1')]
    for name, c in s['caches'].items():
        lines.append(f'  {name[:40]:<42}' + (f"{human_bytes(c['bytes']):>11}   promoted {ago(now-c['time'])} ago"
                                             if c else paint('empty: next trusted job fills it', '2')))
    lines += ['', paint(' burner monitor | status --network | sudo burner pause | journalctl -u burner-slot@\\*', '2')]
    return '\n'.join(lines)


def status(args):
    try:
        s = json.loads((RUN/'status.json').read_text())
    except PermissionError:
        raise SystemExit(f'No permission to read {RUN}/status.json: add yourself to the {GROUP} group '
                         f'(sudo usermod -aG {GROUP},systemd-journal $USER) and log in again.')
    except (OSError, ValueError):
        raise SystemExit(f'No snapshot at {RUN}/status.json: is burner-snapshot.service running?')
    if args.json:
        print(json.dumps(s, indent=1))
        return
    paint = Paint(args.color or sys.stdout.isatty())
    if not args.watch:
        print(render(s, paint, args.network))
        return
    try:
        while True:
            s = json.loads((RUN/'status.json').read_text())
            sys.stdout.write('\033[H\033[2J'+render(s, paint, args.network)+'\n')
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def tmux(*args):
    return subprocess.run(['tmux', *args], check=True, capture_output=True, text=True).stdout.strip()


def monitor(args):
    """A tmux session per user, built only from unprivileged views."""
    if not shutil.which('tmux'):
        raise SystemExit('tmux is not installed.')
    me = shutil.which('burner') or os.path.abspath(sys.argv[0])
    session = 'burner'
    alive = subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True).returncode == 0
    if alive and args.reset:
        tmux('kill-session', '-t', session)
        alive = False
    if not alive:
        overview = tmux('new-session', '-d', '-P', '-F', '#{pane_id}', '-s', session, '-n', 'overview',
                        '-x', '200', '-y', '50', f'{me} status --watch --color')
        filt = tmux('split-window', '-P', '-F', '#{pane_id}', '-h', '-l', '38%', '-t', overview,
                    f'{me} status --watch --color --network')
        res = tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', session, '-n', 'resources',
                   'btop' if shutil.which('btop') else 'top')
        jobs = tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', session, '-n', 'jobs',
                    "journalctl --follow --no-hostname -n 40 -u 'burner-slot@*' -u burner-base -u burner-reaper")
        drops = tmux('split-window', '-P', '-F', '#{pane_id}', '-v', '-l', '35%', '-t', jobs,
                     "journalctl --follow --no-hostname -n 15 --dmesg --grep 'BURNER DROP'")
        for pane, title in ((overview, 'Host / slots / caches'), (filt, 'Network policy'),
                            (res, 'Host resources'), (jobs, 'Controller log'), (drops, 'Blocked by the filter')):
            tmux('select-pane', '-t', pane, '-T', title)
        for option, value in (('mouse', 'on'), ('pane-border-status', 'top'), ('status-left', ' burner | '),
                              ('status-left-length', '20'), ('base-index', '1')):
            tmux('set-option', '-t', session, option, value)
        tmux('move-window', '-r', '-t', session)
        tmux('select-window', '-t', f'{session}:1')
        tmux('select-pane', '-t', overview)
    if os.environ.get('TMUX'):
        os.execvp('tmux', ['tmux', 'switch-client', '-t', session])
    os.execvp('tmux', ['tmux', 'attach-session', '-t', session])


def main():
    p = argparse.ArgumentParser(prog='burner', description='Ephemeral GitHub Actions runners in isolated KVM VMs.')
    sub = p.add_subparsers(dest='command', required=True)
    for name, help_ in (('install', 'firewall, libvirt networks and L2 filter from the configuration'),
                        ('firewall', 'regenerate and load the filter (used by burner-firewall.service)'),
                        ('check', 'test access with each GitHub App'),
                        ('base', 'build, self-test and publish a new base image'),
                        ('selftest', 'test isolation and disposal with the current base'),
                        ('activate', 'start taking jobs'),
                        ('reap', 'periodic cleanup, WAN check and heartbeat (used by a timer)')):
        sub.add_parser(name, help=help_)
    sub.add_parser('pause', help='stop taking jobs').add_argument(
        '--now', action='store_true', help='also cut the jobs that are running')
    sub.add_parser('slot', help='one pass of a slot (used by burner-slot@.timer)').add_argument('n', type=int)
    sub.add_parser('cache-reset', help="the repo's next job starts without cache").add_argument('repo')
    sub.add_parser('snapshot', help='write the status snapshot (used by burner-snapshot.service)').add_argument(
        '--loop', action='store_true')
    e = sub.add_parser('exec', help='run a command in a slot VM, for debugging')
    e.add_argument('n', type=int)
    e.add_argument('argv', nargs=argparse.REMAINDER)
    s = sub.add_parser('status', help='summary (no sudo: burner group)')
    s.add_argument('--network', action='store_true', help='filter counters instead of slots')
    s.add_argument('--watch', action='store_true')
    s.add_argument('--color', action='store_true')
    s.add_argument('--json', action='store_true')
    sub.add_parser('monitor', help='tmux dashboard (no sudo: burner and systemd-journal groups)').add_argument(
        '--reset', action='store_true', help='rebuild the session')
    args = p.parse_args()
    if args.command == 'status':
        return status(args)
    if args.command == 'monitor':
        return monitor(args)
    if os.geteuid() != 0:
        raise SystemExit(f'`burner {args.command}` needs root. Watching does not: burner status / burner monitor.')
    os.umask(0o077)

    def stop(_signal, _frame):
        raise SystemExit('Interrupted: cleaning up what is ours before exiting.')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGALRM, stop)
    cfg = load()
    commands = {'install': install, 'firewall': firewall, 'check': check, 'base': build_base,
                'selftest': selftest, 'activate': activate, 'reap': reap,
                'pause': lambda _: pause(args.now), 'slot': lambda c: slot(c, args.n),
                'cache-reset': lambda c: cache_reset(c, args.repo),
                'snapshot': lambda c: snapshot(c, args.loop),
                'exec': lambda _: exec_in(args.n, args.argv[1:] if args.argv[:1] == ['--'] else args.argv)}
    try:
        commands[args.command](cfg)
    except subprocess.CalledProcessError as error:
        # Never the input: it may carry the registration token.
        event('error', command=error.cmd[0] if isinstance(error.cmd, list) else 'virsh',
              detail=(error.stderr or '')[-1000:])
        raise SystemExit(1) from None
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        event('error', kind=type(error).__name__, detail=str(error)[-2000:])
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
