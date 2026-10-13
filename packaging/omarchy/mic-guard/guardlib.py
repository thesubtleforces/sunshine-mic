"""Shared, standard-library security primitives for Sunshine Mic management."""
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
import re
import stat
import subprocess

UNIT = 'app-dev.lizardbyte.app.Sunshine.service'
HELPER = '/usr/lib/sunshine-mic/guard'
PROFILE = '/etc/sunshine-mic/profile.json'
PENDING = '/etc/sunshine-mic/pending.json'
LOCK = '/run/lock/sunshine-mic.lock'
TABLE = 'sunshine_mic_managed'
INTERFACE = 'tailscale0'
SUDOERS = '/etc/sudoers.d/sunshine-mic-managed'
GUARD_UNIT = 'sunshine-mic-guard.service'
DROPIN = '[Service]\nExecStart=\nExecStart=/usr/bin/sunshine-mic launch\n'
MARKER = 'sunshine-mic schema 1'
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}


class GuardError(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def run(argv, *, data=None, allowed=(0,)):
    try:
        result = subprocess.run(argv, input=data, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=ENV, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError('required command unavailable', 3) from exc
    if result.returncode not in allowed:
        raise GuardError('required command failed', 3)
    return result


def decode_json(text):
    def unique(items):
        out = {}
        for key, value in items:
            if key in out:
                raise GuardError('duplicate JSON key')
            out[key] = value
        return out
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(GuardError('invalid JSON number')))
    except (ValueError, TypeError) as exc:
        raise GuardError('invalid JSON') from exc


def validate_profile(value):
    keys = {'schema', 'uid', 'base_port', 'client_tailscale_ipv4', 'client_lan_ips', 'enabled'}
    if type(value) is not dict or set(value) != keys:
        raise GuardError('invalid profile fields')
    if type(value['schema']) is not int or value['schema'] != 1:
        raise GuardError('unsupported profile schema')
    if type(value['uid']) is not int or value['uid'] <= 0:
        raise GuardError('invalid desktop account')
    try:
        account = pwd.getpwuid(value['uid'])
    except (KeyError, OverflowError) as exc:
        raise GuardError('desktop account does not exist') from exc
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*\$?', account.pw_name):
        raise GuardError('unsupported desktop account name')
    if type(value['base_port']) is not int or not 1029 <= value['base_port'] <= 65514:
        raise GuardError('invalid base port')
    if type(value['enabled']) is not bool:
        raise GuardError('invalid enabled flag')
    if type(value['client_tailscale_ipv4']) is not str:
        raise GuardError('invalid Tailscale peer')
    try:
        peer = ipaddress.IPv4Address(value['client_tailscale_ipv4'])
    except ipaddress.AddressValueError as exc:
        raise GuardError('invalid Tailscale peer') from exc
    if peer not in ipaddress.IPv4Network('100.64.0.0/10'):
        raise GuardError('peer must be a Tailscale IPv4 address')
    addresses = value['client_lan_ips']
    if type(addresses) is not list or len(addresses) > 64:
        raise GuardError('invalid alternate addresses')
    canonical = []
    for address in addresses:
        if type(address) is not str or '%' in address or '/' in address:
            raise GuardError('alternate addresses must be exact IP addresses')
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise GuardError('invalid alternate address') from exc
        if ip.is_unspecified or ip.is_multicast or ip.is_loopback or str(ip) == str(peer):
            raise GuardError('invalid alternate address')
        canonical.append(str(ip))
    if len(set(canonical)) != len(canonical):
        raise GuardError('duplicate alternate address')
    return dict(value, client_tailscale_ipv4=str(peer), client_lan_ips=sorted(canonical))


def trusted_parents(path):
    current = '/'
    for component in os.path.abspath(path).split('/')[1:-1]:
        current = os.path.join(current, component)
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise GuardError('trusted directory unavailable', 3) from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GuardError('unsafe trusted directory')


def trusted_fd(path, *, mode=None):
    trusted_parents(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise GuardError('unsafe trusted file') from exc
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o022 or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        os.close(fd)
        raise GuardError('unsafe trusted file')
    return fd


def trusted_text(path, *, mode=None, missing=False):
    try:
        fd = trusted_fd(path, mode=mode)
    except FileNotFoundError:
        if missing:
            return None
        raise GuardError('required trusted file missing', 3)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        text = stream.read(131073)
    if len(text) > 131072:
        raise GuardError('trusted file too large')
    return text


def read_profile():
    if not os.path.lexists('/etc/sunshine-mic'):
        return None
    text = trusted_text(PROFILE, mode=0o644, missing=True)
    return None if text is None else validate_profile(decode_json(text))


def read_pending():
    if not os.path.lexists('/etc/sunshine-mic'):
        return None
    # Unprivileged launch must refuse the marker without reading root-only intent.
    if os.geteuid() != 0:
        trusted_parents(PENDING)
        if not os.path.lexists(PENDING):
            return None
        info = os.lstat(PENDING)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise GuardError('unsafe transition marker')
        return {'pending': True}
    text = trusted_text(PENDING, mode=0o600, missing=True)
    if text is None:
        return None
    value = decode_json(text)
    if (type(value) is not dict or set(value) != {'schema', 'operation', 'before', 'after'}
            or type(value['schema']) is not int or value['schema'] != 1
            or value['operation'] not in ('configure', 'reconfigure', 'disable', 'reset')):
        raise GuardError('invalid transition marker')
    for key in ('before', 'after'):
        if value[key] is not None:
            value[key] = validate_profile(value[key])
    return value


@contextlib.contextmanager
def trusted_lock(exclusive=False):
    trusted_parents(LOCK)
    if os.geteuid() == 0:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            pass
        else:
            os.fchmod(fd, 0o644)
            os.close(fd)
    try:
        fd = trusted_fd(LOCK, mode=0o644)
    except FileNotFoundError as exc:
        raise GuardError('protection lock unavailable', 3) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        if os.stat(LOCK, follow_symlinks=False).st_ino != os.fstat(fd).st_ino:
            raise GuardError('protection lock replaced')
        yield fd
    finally:
        os.close(fd)


def profile_digest(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def expressions(profile):
    port = profile['base_port'] + 12
    def match(left, right, op='=='):
        return {'match': {'op': op, 'left': left, 'right': right}}
    def payload(protocol, field):
        return {'payload': {'protocol': protocol, 'field': field}}
    udp = match(payload('udp', 'dport'), port)
    result = []
    if profile['enabled']:
        result.append([match({'meta': {'key': 'iifname'}}, INTERFACE),
                       match(payload('ip', 'saddr'), profile['client_tailscale_ipv4']), udp,
                       {'counter': {'packets': 0, 'bytes': 0}}, {'accept': None}])
    result.append([udp, {'counter': {'packets': 0, 'bytes': 0}}, {'drop': None}])
    if profile['enabled']:
        for address in profile['client_lan_ips']:
            protocol = 'ip6' if ':' in address else 'ip'
            for tcp in (profile['base_port'] - 5, profile['base_port'], profile['base_port'] + 21):
                result.append([match({'meta': {'key': 'iifname'}}, INTERFACE, '!='),
                               match(payload(protocol, 'saddr'), address),
                               match(payload('tcp', 'dport'), tcp),
                               {'counter': {'packets': 0, 'bytes': 0}}, {'reject': {'type': 'tcp reset'}}])
    return result


def render_rules(profile):
    profile = validate_profile(profile)
    port = profile['base_port'] + 12
    lines = [f'destroy table inet {TABLE}', f'table inet {TABLE} {{', f' comment "{MARKER}"',
             ' chain input {', '  type filter hook input priority -10; policy accept;']
    if profile['enabled']:
        lines.append(f'  iifname "{INTERFACE}" ip saddr {profile["client_tailscale_ipv4"]} udp dport {port} counter accept')
    lines.append(f'  udp dport {port} counter drop')
    if profile['enabled']:
        for address in profile['client_lan_ips']:
            family = 'ip6' if ':' in address else 'ip'
            for tcp in (profile['base_port'] - 5, profile['base_port'], profile['base_port'] + 21):
                lines.append(f'  iifname != "{INTERFACE}" {family} saddr {address} tcp dport {tcp} counter reject with tcp reset')
    return '\n'.join(lines + [' }', '}', ''])


def apply_profile(profile):
    run(['/usr/bin/nft', '-f', '-'], data=render_rules(profile))


def deny_rules(profiles):
    ports = sorted({p['base_port'] + 12 for p in profiles if p is not None})
    return '\n'.join([f'destroy table inet {TABLE}', f'table inet {TABLE} {{', f' comment "{MARKER}"',
                       ' chain input {', '  type filter hook input priority -10; policy accept;'] +
                      [f'  udp dport {port} counter drop' for port in ports] + [' }', '}', ''])


def kernel_table():
    # Listing all tables distinguishes absence from failed netlink inspection.
    listing = decode_json(run(['/usr/bin/nft', '-j', 'list', 'tables']).stdout)
    entries = listing.get('nftables', [])
    found = any(item.get('table', {}).get('family') == 'inet' and item.get('table', {}).get('name') == TABLE for item in entries)
    if not found:
        return None
    return decode_json(run(['/usr/bin/nft', '-j', 'list', 'table', 'inet', TABLE]).stdout)


def verify_kernel(profile, kernel):
    if kernel is None or type(kernel.get('nftables')) is not list:
        return False
    tables, chains, rules = [], [], []
    for item in kernel['nftables']:
        if 'metainfo' in item:
            continue
        if len(item) != 1:
            return False
        if 'table' in item:
            tables.append(item['table'])
        elif 'chain' in item:
            chains.append(item['chain'])
        elif 'rule' in item:
            rules.append(item['rule'])
        else:
            return False
    if len(tables) != 1 or len(chains) != 1:
        return False
    table, chain = tables[0], chains[0]
    if (table.get('family') != 'inet' or table.get('name') != TABLE or table.get('comment') != MARKER
            or set(table) - {'family', 'name', 'handle', 'comment'}):
        return False
    wanted = {'family': 'inet', 'table': TABLE, 'name': 'input', 'type': 'filter',
              'hook': 'input', 'prio': -10, 'policy': 'accept'}
    if any(chain.get(k) != v for k, v in wanted.items()) or set(chain) - set(wanted) - {'handle'}:
        return False
    expected = expressions(profile)
    if len(rules) != len(expected):
        return False
    for rule, expr in zip(rules, expected):
        if (rule.get('family') != 'inet' or rule.get('table') != TABLE or rule.get('chain') != 'input'
                or set(rule) - {'family', 'table', 'chain', 'handle', 'expr'}):
            return False
        actual = rule.get('expr')
        if type(actual) is not list:
            return False
        cleaned = []
        for part in actual:
            if type(part) is dict and set(part) == {'counter'}:
                value = part['counter']
                if type(value) is not dict or set(value) != {'packets', 'bytes'} or any(type(v) is not int or v < 0 for v in value.values()):
                    return False
                cleaned.append({'counter': {'packets': 0, 'bytes': 0}})
            else:
                cleaned.append(part)
        if cleaned != expr:
            return False
    return True


def validate_peer(profile):
    status = decode_json(run(['/usr/bin/tailscale', 'status', '--json']).stdout)
    if status.get('BackendState') != 'Running':
        raise GuardError('Tailscale is not authenticated', 3)
    link = decode_json(run(['/usr/bin/ip', '-j', 'link', 'show', 'dev', INTERFACE]).stdout)
    if not link or link[0].get('ifname') != INTERFACE:
        raise GuardError('Tailscale interface unavailable', 3)
    peer = profile['client_tailscale_ipv4']
    if peer in status.get('Self', {}).get('TailscaleIPs', []):
        raise GuardError('selected peer is this host')
    peers = status.get('Peer', {})
    matches = [p for p in peers.values() if peer in p.get('TailscaleIPs', [])]
    if len(matches) != 1:
        raise GuardError('selected peer is not in the current tailnet')
    return bool(matches[0].get('Online', False))


def validate_reset_config(text):
    """Accept only absent or unambiguous, single-line disabled mic scalars."""
    seen = set()
    # Sunshine resumes parsing after a list's closing bracket. Never treat a
    # bracketed value with a trailing assignment as one harmless scalar.
    if any(char in text for char in ('\x00', '\\', '{', '}', '[', ']')) or re.search(r'\r(?!\n)', text):
        raise GuardError('ambiguous saved configuration')
    text = text.replace('\r\n', '\n')
    if any((ord(char) < 32 or char.isspace()) and char not in ' \t\n' for char in text):
        raise GuardError('unsupported saved configuration whitespace')
    for raw in text.split('\n'):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r'([A-Za-z_][A-Za-z_0-9]*)\s*=\s*([^\r\n]*)', line)
        if match is None:
            raise GuardError('ambiguous saved configuration')
        key, value = match.groups()
        if key in ('stream_mic', 'mic_default_source'):
            if key in seen or value.strip() != 'disabled':
                raise GuardError('saved microphone settings must be unambiguously disabled')
            seen.add(key)


def verify_disabled_binary():
    data = decode_json(trusted_text('/usr/share/sunshine-mic/disabled-defaults.json', mode=0o644))
    if (type(data) is not dict or set(data) != {'schema', 'sha256', 'stream_mic', 'mic_default_source'}
            or type(data['schema']) is not int or data['schema'] != 1
            or data['stream_mic'] is not False or data['mic_default_source'] is not False
            or type(data['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', data['sha256'])):
        raise GuardError('disabled-default binary attestation invalid')
    try:
        fd = trusted_fd('/usr/bin/sunshine')
    except FileNotFoundError as exc:
        raise GuardError('supported binary unavailable', 3) from exc
    with os.fdopen(fd, 'rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != data['sha256']:
        raise GuardError('binary does not match disabled-default attestation')
