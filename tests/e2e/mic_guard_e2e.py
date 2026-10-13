#!/usr/bin/env python3
"""Exercise guard validation and real nft packet delivery in private namespaces.

No production helper is invoked. A fresh user/network/mount/PID namespace hides
host state and devices before importing the copied guard library. Missing
isolation is a failure, never a skip. Transaction tests inject unavailable
service/peer boundaries; real systemd/pacman/reboot and iPad remain VM gates.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys

sys.dont_write_bytecode = True


def run(*args, input=None):
    """Run a real binary and fail with its captured diagnostic."""
    return subprocess.run(args, input=input, text=True, capture_output=True, check=True)


def isolate():
    """Make mounts private/read-only, then hide all mutable production state."""
    run('/usr/bin/mount', '--make-rprivate', '/')
    run('/usr/bin/mount', '--rbind', '/', '/')
    run('/usr/bin/mount', '-o', 'remount,bind,ro', '/')
    for target in ('/etc', '/run', '/var', '/home', '/root', '/tmp', '/dev', '/sys', '/usr/local'):
        if Path(target).exists():
            run('/usr/bin/mount', '-t', 'tmpfs', '-o', 'mode=0755,nosuid,nodev', 'tmpfs', target)
            if not os.path.ismount(target):
                raise RuntimeError('production mount was not hidden: ' + target)
    Path('/var/run').symlink_to('/run')
    Path('/etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\ntester:x:1000:1000:tester:/home/tester:/bin/sh\n')
    Path('/etc/group').write_text('root:x:0:\ntester:x:1000:\n')
    Path('/etc/nsswitch.conf').write_text('passwd: files\ngroup: files\nhosts: files\n')
    Path('/run/lock').mkdir()
    # External commands in this test have piped stdio; no host device is exposed.
    run('/usr/bin/ip', 'link', 'set', 'lo', 'up')


def topology():
    """Create distinct Tailscale/LAN interfaces and IPv4/IPv6 senders."""
    for ns, host, h4, p4, h6, p6 in (
        ('ts', 'tailscale0', '100.64.0.1/24', '100.64.0.2/24', 'fd00::1/64', 'fd00::2/64'),
        ('lan', 'lan0', '192.0.2.1/24', '192.0.2.2/24', 'fd01::1/64', 'fd01::2/64'),
    ):
        run('/usr/bin/ip', 'netns', 'add', ns)
        run('/usr/bin/ip', 'link', 'add', host, 'type', 'veth', 'peer', 'name', 'peer-'+ns)
        run('/usr/bin/ip', 'link', 'set', 'peer-'+ns, 'netns', ns)
        run('/usr/bin/ip', 'addr', 'add', h4, 'dev', host)
        run('/usr/bin/ip', '-6', 'addr', 'add', h6, 'dev', host, 'nodad')
        run('/usr/bin/ip', 'link', 'set', host, 'up')
        for argv in (
            ('link', 'set', 'lo', 'up'),
            ('addr', 'add', p4, 'dev', 'peer-'+ns),
            ('-6', 'addr', 'add', p6, 'dev', 'peer-'+ns, 'nodad'),
            ('link', 'set', 'peer-'+ns, 'up'),
        ):
            run('/usr/bin/ip', '-n', ns, *argv)
    run('/usr/bin/ip', '-n', 'ts', 'addr', 'add', '100.64.0.3/24', 'dev', 'peer-ts')
    run('/usr/bin/ip', '-n', 'lan', 'addr', 'add', '100.64.0.2/32', 'dev', 'peer-lan')
    for interface in ('all', 'default', 'tailscale0', 'lan0'):
        Path('/proc/sys/net/ipv4/conf/'+interface+'/rp_filter').write_text('0')


SEND = '''import socket,sys
family=socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET
s=socket.socket(family,socket.SOCK_DGRAM)
s.bind((sys.argv[2],0));s.sendto(b'guard-contract',(sys.argv[1],int(sys.argv[3])))
'''
CONNECT = '''import socket,sys
s=socket.socket(socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET)
s.settimeout(0.5)
try:s.connect((sys.argv[1],int(sys.argv[2])));print('delivered')
except OSError:print('denied')
'''


def delivered(ns, destination, source, port):
    """Measure actual UDP delivery, not generated nft text."""
    with socket.socket(socket.AF_INET6 if ':' in destination else socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((destination, port))
        sock.settimeout(0.25)
        run('/usr/bin/ip', 'netns', 'exec', ns, sys.executable, '-I', '-c', SEND, destination, source, str(port))
        try:
            return sock.recv(100) == b'guard-contract'
        except TimeoutError:
            return False


def tcp_delivered(ns, destination, port):
    """Measure connection acceptance through the real input chain."""
    with socket.socket(socket.AF_INET6 if ':' in destination else socket.AF_INET) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((destination, port))
        sock.listen()
        result = run('/usr/bin/ip', 'netns', 'exec', ns, sys.executable, '-I', '-c', CONNECT, destination, str(port))
        return result.stdout.strip() == 'delivered'


def lock_contracts(lib):
    """Exercise exclusion before exec and lock release on actual exec."""
    read_fd, write_fd = os.pipe()
    with lib.trusted_lock() as inherited:
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            os.close(inherited)
            os.write(write_fd,b'attempt')
            with lib.trusted_lock(exclusive=True):
                os.write(write_fd,b'acquired')
            os._exit(0)
        os.close(write_fd)
        assert os.read(read_fd,7) == b'attempt'
        assert not select.select([read_fd],[],[],0.1)[0], 'mutation raced shared launch lock'
    assert select.select([read_fd],[],[],5)[0], 'mutation remained blocked after launch'
    assert os.read(read_fd,8) == b'acquired'
    os.close(read_fd)
    assert os.waitpid(child,0)[1] == 0
    child = os.fork()
    if child == 0:
        with lib.trusted_lock():
            os.execv('/usr/bin/python3',['python3','-I','-c',
                'import fcntl; f=open(\"/run/lock/sunshine-mic.lock\"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'])
    assert os.waitpid(child,0)[1] == 0, 'exec leaked shared lock and blocks disable'
    print('PASS mutation exclusion during shared launch gate and release on actual exec',flush=True)


def transition_failure_contracts(lib, guard, profile):
    """Exercise durable recovery with real files/nft and injected service failure."""
    import contextlib
    import fcntl
    import io

    Path('/etc/sudoers.d').mkdir(mode=0o755)
    originals = {name: guard[name] for name in ('run', 'stopped_checks', 'unit_checks', 'validate_peer')}
    fail_activation = True

    def service_boundary(args, **kwargs):
        if args[0] != '/usr/bin/systemctl':
            return originals['run'](args, **kwargs)
        # Starting the oneshot must occur outside LOCK_EX, or apply deadlocks.
        with open('/run/lock/sunshine-mic.lock') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if fail_activation:
            raise lib.GuardError('injected system activation failure', 3)
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    guard['run'] = service_boundary
    guard['stopped_checks'] = lambda *_: '/home/tester/.config'
    guard['unit_checks'] = lambda *_, **__: '/home/tester/.config'
    guard['validate_peer'] = lambda _: True
    try:
        try:
            guard['mutate']('configure', profile['uid'], profile)
        except lib.GuardError as error:
            assert 'activation incomplete' in str(error)
        else:
            raise AssertionError('failed system activation accepted as complete')
        intent = lib.read_pending()
        assert intent['operation'] == 'configure' and intent['after'] == profile
        assert lib.read_profile() == profile
        assert lib.verify_kernel(profile | {'enabled': False}, lib.kernel_table())
        assert not delivered('ts', '100.64.0.1', '100.64.0.2', profile['base_port'] + 12)
        try:
            guard['mutate']('reconfigure', profile['uid'], profile | {'base_port': 43000})
        except lib.GuardError:
            pass
        else:
            raise AssertionError('different operation replaced incomplete intent')
        assert lib.read_pending() == intent
        old_argv = sys.argv
        sys.argv = ['guard', 'apply']
        try:
            try:
                guard['main']()
            except lib.GuardError:
                pass
            else:
                raise AssertionError('apply accepted incomplete activation')
        finally:
            sys.argv = old_argv
        fail_activation = False
        with contextlib.redirect_stdout(io.StringIO()):
            assert guard['mutate']('configure', profile['uid'], profile) == 0
        assert lib.read_pending() is None
        assert lib.verify_kernel(profile, lib.kernel_table())
        assert delivered('ts', '100.64.0.1', '100.64.0.2', profile['base_port'] + 12)
        with contextlib.redirect_stdout(io.StringIO()):
            assert guard['mutate']('disable', profile['uid'], None) == 0
        assert lib.read_profile()['enabled'] is False and lib.read_pending() is None
        assert lib.verify_kernel(profile | {'enabled': False}, lib.kernel_table())
        assert not delivered('ts', '100.64.0.1', '100.64.0.2', profile['base_port'] + 12)
    finally:
        guard.update(originals)
    print('PASS injected activation failure retains deny/intent, blocks apply/conflicting recovery, identical recovery and disable; NOT real systemd qualification', flush=True)


def inner(library):
    """Load source before hiding the checkout, then exercise isolated behavior."""
    spec = importlib.util.spec_from_file_location('guardlib', library)
    lib = importlib.util.module_from_spec(spec)
    sys.modules['guardlib'] = lib
    spec.loader.exec_module(lib)
    guard = {'__name__':'guard_contract'}
    exec(compile((library.parent/'guard').read_bytes(),str(library.parent/'guard'),'exec'),guard)
    isolate()
    p = dict(schema=1, uid=1000, base_port=42123, client_tailscale_ipv4='100.64.0.2',
             client_lan_ips=['192.0.2.2', 'fd01::2'], enabled=True)
    lib.validate_profile(p)
    cases = [dict(schema=True), dict(schema=2), dict(uid=True), dict(uid=0), dict(uid=99999),
             dict(base_port=True), dict(base_port=1028), dict(base_port=65515),
             dict(enabled=1), dict(client_tailscale_ipv4='100.64.0.2/32'),
             dict(client_tailscale_ipv4='::1'), dict(client_lan_ips=['192.0.2.2/32']),
             dict(client_lan_ips=['192.0.2.2','192.0.2.2']), dict(extra='rejected')]
    for change in cases:
        bad = p | change
        try:
            lib.validate_profile(bad)
        except lib.GuardError:
            continue
        raise AssertionError('accepted malformed profile: '+repr(change))
    for text in ('', '# microphone disabled by default\n', 'stream_mic = disabled\nmic_default_source = disabled\n'):
        lib.validate_reset_config(text)
    for text in ('stream_mic = enabled\n', 'stream_mic = disabled\nstream_mic = disabled\n',
                 'mic_default_source = [\ndisabled\n]\n', 'stream_mic = disabled enabled\n',
                 'prep_cmd = [] stream_mic = enabled\n', 'x = value\rstream_mic = enabled\n'):
        try:
            lib.validate_reset_config(text)
        except lib.GuardError:
            continue
        raise AssertionError('accepted ambiguous/enabled reset config')
    print('PASS profile type/range/peer boundaries and conservative reset scalars', flush=True)
    directory = Path('/etc/sunshine-mic')
    directory.mkdir(mode=0o755)
    profile_file = directory/'profile.json'
    profile_file.write_text(json.dumps(p))
    profile_file.chmod(0o644)
    assert lib.read_profile() == lib.validate_profile(p)
    for mode in (0o666, 0o600):
        profile_file.chmod(mode)
        try:
            lib.read_profile()
        except lib.GuardError:
            pass
        else:
            raise AssertionError('unsafe profile mode accepted')
    profile_file.chmod(0o644)
    profile_file.rename(directory/'original')
    profile_file.symlink_to(directory/'original')
    try:
        lib.read_profile()
    except lib.GuardError:
        pass
    else:
        raise AssertionError('symlink profile accepted')
    profile_file.unlink()
    os.link(directory/'original', profile_file)
    try:
        lib.read_profile()
    except lib.GuardError:
        pass
    else:
        raise AssertionError('hardlinked profile accepted')
    profile_file.unlink()
    (directory/'original').rename(profile_file)
    marker = directory/'pending.json'
    marker.write_text(json.dumps(dict(schema=1, operation='configure', before=None, after=p)))
    marker.chmod(0o600)
    assert lib.read_pending()['operation'] == 'configure'
    marker.unlink()
    print('PASS trusted profile modes, symlink/hardlink rejection and durable intent decoding',flush=True)
    with lib.trusted_lock():
        pass
    lock_contracts(lib)
    child = os.fork()
    if child == 0:
        os.setgid(1000)
        os.setuid(1000)
        try:
            guard['main']()
        except lib.GuardError as error:
            os._exit(0 if error.code == 2 else 1)
        os._exit(1)
    assert os.waitpid(child,0)[1] == 0, 'unprivileged direct helper accepted'
    old_argv = sys.argv
    sys.argv = ['guard','apply','extra']
    try:
        guard['main']()
    except lib.GuardError as error:
        assert error.code == 2
    else:
        raise AssertionError('extra root helper arguments accepted')
    finally:
        sys.argv = old_argv
    print('PASS root helper privilege and exact-command boundaries',flush=True)
    topology()
    run('/usr/bin/nft', '-f', '-', input='table inet unrelated {\n chain input {\n type filter hook input priority 0; policy accept;\n }\n}\n')
    lib.apply_profile(p)
    assert lib.verify_kernel(p,lib.kernel_table()), 'freshly applied kernel rules not verified'
    mic = p['base_port'] + 12
    paths = [('ts','100.64.0.1','100.64.0.2',True), ('ts','100.64.0.1','100.64.0.3',False),
             ('lan','192.0.2.1','192.0.2.2',False), ('lan','192.0.2.1','100.64.0.2',False),
             ('ts','fd00::1','fd00::2',False), ('lan','fd01::1','fd01::2',False)]
    for ns, destination, source, expected in paths:
        assert delivered(ns,destination,source,mic) == expected, (ns,source,'microphone delivery')
        assert delivered(ns,destination,source,mic+1), (ns,source,'unrelated port blocked')
    for port in (p['base_port']-5,p['base_port'],p['base_port']+21):
        assert not tcp_delivered('lan','192.0.2.1',port)
        assert not tcp_delivered('lan','fd01::1',port)
        assert tcp_delivered('ts','100.64.0.1',port)
    run('/usr/bin/nft','list','table','inet','unrelated')
    lib.apply_profile(p)
    assert delivered('ts','100.64.0.1','100.64.0.2',mic)
    # An invalid atomic reload cannot erase the old protection.
    invalid = subprocess.run(['/usr/bin/nft','-f','-'],input='destroy table inet sunshine_mic_managed\ntable inet sunshine_mic_managed { INVALID; }\n',text=True,capture_output=True)
    assert invalid.returncode != 0
    assert not delivered('lan','192.0.2.1','192.0.2.2',mic)
    assert lib.verify_kernel(p,lib.kernel_table()), 'counter traffic or failed reload invalidated correct protection'
    run('/usr/bin/nft','insert','rule','inet',lib.TABLE,'input','udp','dport',str(mic),'accept')
    assert not lib.verify_kernel(p,lib.kernel_table()), 'unsafe kernel drift reported healthy'
    print('PASS real selected-peer/interface, spoofed/LAN/IPv6 denial, TCP exclusions, non-default base, unrelated rules/ports, idempotence, failed atomic reload',flush=True)
    lib.apply_profile(p | {'enabled':False})
    assert lib.verify_kernel(p | {'enabled':False},lib.kernel_table()), 'disabled protection not verified'
    for ns, destination, source, _ in paths:
        assert not delivered(ns,destination,source,mic), (ns,source,'disabled microphone delivered')
    assert tcp_delivered('lan','192.0.2.1',p['base_port'])
    run('/usr/bin/nft','list','table','inet','unrelated')
    print('PASS disabled policy denies all microphone paths and removes only alternate TCP exclusions',flush=True)
    transition_failure_contracts(lib, guard, p)
    print('NOT COVERED: real sudo/systemd activation, full transition/startup races, pacman lifecycle/reboot, real iPad audio; these remain required release gates.',flush=True)
    return 0


def main():
    """Refuse root outer execution and require real namespaces, never skip."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inner', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--library',type=Path,default=Path(__file__).resolve().parents[2]/'packaging/omarchy/mic-guard/guardlib.py')
    args = parser.parse_args()
    if args.inner:
        # Do not trust an environment flag or root UID as namespace proof.
        if os.getuid()!=0 or Path('/proc/self/uid_map').read_text().split()[0:3] == ['0','0','4294967295']:
            raise RuntimeError('isolated user namespace required')
        return inner(args.library)
    if os.geteuid()==0:
        parser.error('run as an unprivileged user; never run this against production root')
    for tool in ('unshare','mount','ip','nft'):
        if not shutil.which(tool):
            raise RuntimeError('missing required isolation tool: '+tool)
    command=['/usr/bin/unshare','--user','--map-auto','--map-root-user','--net','--mount','--pid','--fork','--mount-proc',
             sys.executable,'-I',str(Path(__file__).resolve()),'--inner','--library',str(args.library.resolve())]
    return subprocess.run(command).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError,RuntimeError,subprocess.CalledProcessError) as exc:
        print('FAIL: required isolated guard verification unavailable: '+str(exc),file=sys.stderr)
        if isinstance(exc,subprocess.CalledProcessError):
            print(exc.stderr,file=sys.stderr)
        raise SystemExit(3)
