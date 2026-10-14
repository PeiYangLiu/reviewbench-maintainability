"""Offline ownership/ORIGINAL observer tests. No Docker, cloud or model launch.

The filesystem and /proc fixtures are disposable; ORIGINAL source is read from
its pinned local kit, never changed. The real child test only surveys /proc.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[1]
NODE = PROJECT / '.benchmarks/framework-repair-20261012-v1/integration/production-live/native-waves/0003/cohort-task-pins-v2/lanes/0e54eb1609360a93be55/node'
C3 = PROJECT / '.benchmarks/parallel-takeover-20261012/closure-20261012/c3-recovery/INDEX.json'


def module(raw, path):
    value = ModuleType(path.stem)
    value.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), value.__dict__)
    return value


T = module((PROJECT/'scripts/agentic_owned_runtime_namespace.py').read_bytes(),
           PROJECT/'scripts/agentic_owned_runtime_namespace.py')
G = module((PROJECT/'scripts/agentic_node_transform.py').read_bytes(), PROJECT/'scripts/agentic_node_transform.py')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pin(path):
    raw = path.read_bytes()
    return dict(path=str(path), bytes=len(raw), sha256=sha(raw))


def runtime():
    scope = {'__file__': str(PROJECT/'scripts/agentic_owned_runtime_namespace.py'),
             '_ORN_RUNTIME_SOURCE': T.RUNTIME_SOURCE}
    exec(compile(T.RUNTIME_SOURCE, scope['__file__'], 'exec'), scope)
    return scope


@pytest.fixture
def original(monkeypatch):
    if not (NODE/'DESCRIPTOR.json').exists():
        pytest.skip('read-only pinned original observer kit not available')
    descriptor = json.loads((NODE/'DESCRIPTOR.json').read_bytes())
    plan = json.loads(Path(descriptor['canonical_plan']['path']).read_bytes())
    kit = Path(descriptor['canonical_plan']['path']).parent
    raw = (kit/'reuse/integration_common.py').read_bytes()
    assert sha(raw) == plan['files']['reuse/integration_common.py']['sha256']
    common = module(raw, kit/'reuse/integration_common.py')
    monkeypatch.setitem(sys.modules, 'integration_common', common)
    observe = module((kit/'reuse/node_observe.py').read_bytes(), kit/'reuse/node_observe.py')
    probe = (kit/'reuse/process_probe.py').read_text()
    assert sha(probe.encode()) == T.PROCESS_SHA256
    return SimpleNamespace(descriptor=descriptor, plan=plan, kit=kit, observe=observe, probe=probe)


def dump(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + '\n')


def proc_entry(proc, pid, command, cwd, fd_path=None, start='12345'):
    entry = proc/str(pid)
    (entry/'fd').mkdir(parents=True)
    (entry/'ns').mkdir()
    fields = ['S', '1', str(pid)] + ['0']*16 + [start]
    (entry/'stat').write_text(str(pid)+' (dockerd) '+' '.join(fields))
    (entry/'cmdline').write_bytes(b'\0'.join(os.fsencode(x) for x in command)+b'\0')
    (entry/'cwd').symlink_to(cwd)
    (entry/'exe').symlink_to('/usr/bin/dockerd')
    for name in ('mnt', 'net'):
        (entry/'ns'/name).symlink_to(name+':[1]')
    if fd_path is not None:
        (entry/'fd/3').symlink_to(fd_path)
    return entry


@pytest.fixture
def owned():
    # Short disposable prefix keeps actual sockaddr_un limits meaningful.
    with tempfile.TemporaryDirectory(prefix='orn-') as td:
        base = Path(td)
        shared = base/'s'
        root = shared/'j/t'
        (root/'ii').mkdir(parents=True)
        (root/'x').mkdir()
        endpoint = shared/'.exec'/sha(os.fsencode(root))[:32]
        endpoint.mkdir(parents=True, mode=0o700)
        reservation = dict(schema='reviewbench-native-exec-root/v1', root=str(root),
                           exec_root=str(endpoint), original_exec_root=str(root/'x'),
                           native_authorized=False, automatic_retry=False)
        dump(endpoint/'OWNER.json', reservation)
        dump(root/'ii/NATIVE_EXEC_ROOT.json', reservation)
        paths = dict(base=root/'ii', data=root/'ii/d', exec=root/'x', socket=root/'ii/s',
                     pid=root/'ii/p', config=root/'ii/daemon.json')
        command = ['/usr/bin/dockerd', '--config-file', str(paths['config'])]
        owner = dict(pid=100, proc_start_ticks='12345', command=command, root=str(root),
                     paths={key: str(value) for key, value in paths.items()})
        dump(root/'ii/OWNED_DAEMON.json', owner)
        config = {'exec-root': str(endpoint), 'data-root': str(root/'ii/d'),
                  'pidfile': str(root/'ii/p'), 'hosts': ['unix://'+str(root/'ii/s')],
                  'containerd': str(root/'x/containerd/containerd.sock')}
        dump(root/'ii/daemon.json', config)
        (root/'ii/p').write_text('100\n')
        proc = base/'proc'
        (proc/'self/ns').mkdir(parents=True)
        for name in ('mnt', 'net'):
            (proc/'self/ns'/name).symlink_to(name+':[1]')
        entry = proc_entry(proc, 100, command, root, endpoint/'extensions/callback.sock')
        scope = runtime()
        scope['_ORN_PROC'] = proc
        yield SimpleNamespace(base=base, shared=shared, root=root, endpoint=endpoint,
                              proc=proc, entry=entry, scope=scope, owner=owner,
                              config=config, reservation=reservation, command=command)


def survey(original, owned, modified=True):
    if modified:
        fn = owned.scope['_orn_survey'](original.probe, {'__file__': str(original.kit/'reuse/process_probe.py')})
    else:
        fn = module(original.probe.encode(), original.kit/'reuse/process_probe.py').survey
    fn.__globals__['Path'] = lambda value: owned.proc if str(value) == '/proc' else Path(value)
    return fn(owned.shared, owned.root)


def clean_observation(owned, rows, unknown, processes):
    return dict(root=str(owned.root), node={}, unknown_namespace=unknown,
                legacy_roots=rows, processes=processes, containers=[], gate=dict(state='absent'),
                stable_scratch=dict(exists=False, symlink=False, canonical=True))


def test_original_classifier_and_original_probe_both_conflict_then_owned_copy_passes(original, owned):
    rows, unknown = original.observe.legacy_roots(owned.shared, owned.root)
    before = survey(original, owned, modified=False)
    assert unknown == [dict(path=str(owned.endpoint), reason='legacy identity config absent/unsafe')]
    assert before['legacy_references'][0]['roots'] == [str(owned.endpoint)]
    with pytest.raises(RuntimeError, match='unknown node/process/daemon facts; no bypass'):
        original.observe.clean_fact(clean_observation(owned, rows, unknown, before), original.plan['node_resource_limits'])
    observe = owned.scope['_orn_observer'](original.observe, original.kit, original.plan)
    after = survey(original, owned)
    rows, unknown = observe.legacy_roots(owned.shared, owned.root)
    assert rows == unknown == after['legacy_references'] == after['unknown'] == []
    assert after['docker_daemons'] == before['docker_daemons']
    assert after['owned_runtime_namespace']['pid'] == 100
    assert observe.clean_fact(clean_observation(owned, rows, unknown, after), original.plan['node_resource_limits']) == 'initialize_verified_virgin'
    # Canonical imported functions/globals are not patched by the namespace copy.
    assert original.observe.legacy_roots(owned.shared, owned.root)[1]


@pytest.mark.parametrize('damage', [
    'sibling', 'other-hash', 'base-symlink', 'endpoint-symlink', 'internal-symlink',
    'owner-symlink', 'owner-hardlink', 'owner-mode', 'root-mode', 'base-mode', 'endpoint-mode',
    'missing-owner', 'missing-mirror', 'mirror-drift', 'native-authorized',
    'config-exec', 'config-data', 'config-host', 'foreign-containerd', 'duplicate-config-key',
    'pidfile', 'owner-root', 'owner-pid-bool', 'owner-paths', 'owner-command',
    'dead-daemon', 'pid-start', 'pgrp', 'live-command', 'foreign-exe', 'foreign-net',
])
def test_namespace_ownership_negatives_fail_closed(original, owned, damage):
    e, r = owned.endpoint, owned.root
    if damage == 'sibling': (e.parent/'unexpected').mkdir()
    elif damage == 'other-hash': e.rename(e.with_name('0'*32))
    elif damage in ('base-symlink', 'endpoint-symlink'):
        path = e.parent if damage == 'base-symlink' else e
        moved = owned.base/('moved-'+damage)
        path.rename(moved); path.symlink_to(moved, target_is_directory=True)
    elif damage == 'internal-symlink': (e/'foreign').symlink_to(r)
    elif damage == 'owner-symlink':
        (e/'OWNER.json').unlink(); (e/'OWNER.json').symlink_to(r/'ii/NATIVE_EXEC_ROOT.json')
    elif damage == 'owner-hardlink': os.link(e/'OWNER.json', owned.base/'linked-owner')
    elif damage in ('owner-mode', 'root-mode', 'base-mode', 'endpoint-mode'):
        path = {'owner-mode': e/'OWNER.json', 'root-mode': r, 'base-mode': e.parent, 'endpoint-mode': e}[damage]
        path.chmod(0o777)
    elif damage.startswith('missing-'):
        (e/'OWNER.json' if damage == 'missing-owner' else r/'ii/NATIVE_EXEC_ROOT.json').unlink()
    elif damage == 'mirror-drift': dump(r/'ii/NATIVE_EXEC_ROOT.json', {**owned.reservation, 'root': str(r)+'-other'})
    elif damage == 'native-authorized':
        for path in (e/'OWNER.json', r/'ii/NATIVE_EXEC_ROOT.json'):
            dump(path, {**owned.reservation, 'native_authorized': True})
    elif damage.startswith('config-') or damage == 'foreign-containerd':
        key = {'config-exec': 'exec-root', 'config-data': 'data-root', 'config-host': 'hosts', 'foreign-containerd': 'containerd'}[damage]
        dump(r/'ii/daemon.json', {**owned.config, key: '/foreign'})
    elif damage == 'duplicate-config-key':
        raw = (r/'ii/daemon.json').read_text(); (r/'ii/daemon.json').write_text(raw[:-2]+',"hosts": []}\n')
    elif damage == 'pidfile': (r/'ii/p').write_text('101')
    elif damage.startswith('owner-'):
        key, value = {'owner-root': ('root', '/foreign'), 'owner-pid-bool': ('pid', True),
                      'owner-paths': ('paths', {}), 'owner-command': ('command', ['/tmp/dockerd'])}[damage]
        dump(r/'ii/OWNED_DAEMON.json', {**owned.owner, key: value})
    elif damage in ('dead-daemon', 'pid-start', 'pgrp'):
        path = owned.entry/'stat'; raw = path.read_text()
        if damage == 'dead-daemon': raw = raw.replace(') S ', ') Z ')
        elif damage == 'pid-start': raw = raw[:-5]+'12346'
        else: raw = raw.replace(') S 1 100 ', ') S 1 101 ')
        path.write_text(raw)
    elif damage == 'live-command': (owned.entry/'cmdline').write_bytes(b'/usr/bin/not-dockerd\0')
    elif damage in ('foreign-exe', 'foreign-net'):
        path = owned.entry/('exe' if damage == 'foreign-exe' else 'ns/net')
        path.unlink(); path.symlink_to('/other' if damage == 'foreign-exe' else 'net:[2]')
    with pytest.raises((ValueError, OSError)):
        owned.scope['_orn_proof'](owned.shared, owned.root)
    # Failure is shared by the namespace and ORIGINAL process-copy entrypoints.
    with pytest.raises((ValueError, OSError)):
        survey(original, owned)


@pytest.mark.parametrize('foreign_kind', ['current-exec', 'other-exec', 'legacy-root', 'prefix-collision'])
def test_foreign_processes_not_whitelisted(original, owned, foreign_kind):
    ref = {'current-exec': owned.endpoint, 'other-exec': owned.endpoint.parent/('0'*32),
           'legacy-root': owned.shared/'old/task', 'prefix-collision': Path(str(owned.endpoint)+'-other')}[foreign_kind]
    proc_entry(owned.proc, 200, ['/usr/bin/foreign'], owned.root, ref/'file')
    result = survey(original, owned)
    if foreign_kind == 'current-exec':
        assert result['unknown'][0]['pid'] == 200
        assert 'unowned process' in result['unknown'][0]['detail']
    else:
        assert result['legacy_references'][-1]['pid'] == 200
        assert result['legacy_references'][-1]['roots'] == [str(ref)]
    with pytest.raises(RuntimeError):
        original.observe.clean_fact(clean_observation(owned, [], [], result), original.plan['node_resource_limits'])


def test_foreign_uid_and_cross_device_rejected(owned, monkeypatch):
    orig = Path.lstat
    def foreign(path, *args, **kwargs):
        st = orig(path, *args, **kwargs)
        if path == owned.endpoint:
            values = {name: getattr(st, name) for name in dir(st) if name.startswith('st_')}
            values['st_uid'] += 1
            return SimpleNamespace(**values)
        return st
    monkeypatch.setattr(Path, 'lstat', foreign)
    with pytest.raises(ValueError, match='ownership'):
        owned.scope['_orn_proof'](owned.shared, owned.root)
    monkeypatch.setattr(Path, 'lstat', orig)
    with pytest.raises(ValueError, match='device'):
        owned.scope['_orn_dir'](owned.endpoint, device=owned.endpoint.stat().st_dev+1)


def test_unknown_namespace_sibling_still_hits_original_guard(original, owned):
    (owned.shared/'foreign-job/foreign-task').mkdir(parents=True)
    observer = owned.scope['_orn_observer'](original.observe, original.kit, original.plan)
    rows, unknown = observer.legacy_roots(owned.shared, owned.root)
    assert unknown == [dict(path=str(owned.shared/'foreign-job/foreign-task'), reason='legacy identity config absent/unsafe')]
    with pytest.raises(RuntimeError, match='unknown node/process'):
        observer.clean_fact(clean_observation(owned, rows, unknown, survey(original, owned)), original.plan['node_resource_limits'])


def test_original_gate_resource_and_live_container_checks_remain(original, owned):
    observer = owned.scope['_orn_observer'](original.observe, original.kit, original.plan)
    assert observer.clean_fact.__code__.co_code == original.observe.clean_fact.__code__.co_code
    observation = clean_observation(owned, [], [], survey(original, owned))
    limits = original.plan['node_resource_limits']
    observation['gate'] = dict(state='present', value=dict(leases={}, limits=limits, storage_path=str(owned.shared/'node-shared-scratch-v1')))
    assert observer.clean_fact(observation, limits) == 'retain_existing_stable_gate'
    for mutate, message in [
        (lambda o: o['gate']['value'].update(limits={**limits, 'cpus': 0}), 'resource limits differ'),
        (lambda o: o['gate']['value'].update(leases={'foreign': {}}), 'nonempty/unknown leases'),
        (lambda o: o.update(containers=[dict(state=dict(Running=True))]), 'live/unknown container'),
        (lambda o: o['processes']['unknown'].append(dict(reason='permission')), 'unknown node/process')]:
        changed = copy.deepcopy(observation); mutate(changed)
        with pytest.raises(RuntimeError, match=message): observer.clean_fact(changed, limits)


def test_capture_isolated_copy_and_main_diagnostic_reused_seam(original, owned, monkeypatch):
    limits = original.plan['node_resource_limits']
    plan = {**original.plan, 'mapping': {'root': str(owned.root)}}
    original.observe.identity = lambda root: dict(node_id='fixture-actual')
    (owned.root/'RUNTIME_READY.json').write_text('{}')
    (owned.root/'ii/RESULT.json').write_text('{}')
    observer = owned.scope['_orn_observer'](original.observe, original.kit, plan)
    raw_capture = original.observe.capture
    processes = survey(original, owned)
    seen = []
    def run(argv):
        seen.append(argv)
        return dict(exit_status=0, stdout=json.dumps(processes) if argv[0] == sys.executable else '',
                    stderr='', observer_timed_out=False)
    # Real Unix sockets on disposable paths; no Docker API or TCP connection.
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(owned.root/'ii/s'))
        result = observer.capture(owned.root, {'limits': limits}, {'DOCKER_HOST': 'unix://'+str(owned.root/'ii/s')}, run=run)
    assert result['owned_runtime_namespace']['pid'] == 100
    assert result['node'] == {'node_id': 'fixture-actual'}
    assert original.observe.capture is raw_capture
    assert seen[0][:3] == [sys.executable, '-B', '-c']
    assert '_orn_probe_cli()' in seen[0][3]
    assert any(argv[:3] == ['/usr/bin/docker', '--host', 'unix://'+str(owned.root/'ii/s')] for argv in seen)
    assert observer.critical(result)['owned_runtime_namespace'] == result['owned_runtime_namespace']
    # Main's actual diagnostic helper wraps/reverts reused and capture in finally.
    diagnostic_path = PROJECT/'scripts/agentic_templates/node_diagnostics.py'
    if diagnostic_path.exists():
        diag = module(diagnostic_path.read_bytes(), diagnostic_path)
        diag._rbdiag_begin(owned.root, 'fixture')
        def reused(*args): return 'paths', observer
        scope = {'reused': reused}
        exec('def node_clean(root, plan, kit, env):\n p,o=reused(kit,plan)\n v=o.capture(root,{},env)\n return p,v,o.clean_fact(v,plan["node_resource_limits"])', scope)
        physical = SimpleNamespace(node_clean=scope['node_clean'])
        original_capture = observer.capture
        observer.capture = lambda *a, **k: result
        wrapped = observer.capture
        outcome = diag._rbdiag_node_clean(physical, owned.root, plan, original.kit, {})
        assert outcome[2] == 'initialize_verified_virgin'
        assert scope['reused'] is reused and observer.capture is wrapped
        assert list((owned.root/'diagnostics-runtime').glob('*OBSERVATION*'))
        observer.capture = original_capture


@pytest.mark.parametrize('failure', ['container-count', 'duplicate-container', 'process-size', 'process-stderr', 'socket-symlink'])
def test_original_capture_socket_and_output_limits(original, owned, failure):
    plan = {**original.plan, 'mapping': {'root': str(owned.root)}}
    original.observe.identity = lambda root: dict(node_id='fixture')
    (owned.root/'RUNTIME_READY.json').write_text('{}')
    (owned.root/'ii/RESULT.json').write_text('{}')
    observe = owned.scope['_orn_observer'](original.observe, original.kit, plan)
    processes = survey(original, owned)
    def run(argv):
        event = dict(exit_status=0, stdout='', stderr='', observer_timed_out=False)
        if argv[0] == sys.executable:
            event['stdout'] = json.dumps(processes)
            if failure == 'process-size': event['stdout'] = ' '* (4*1024**2+1)
            if failure == 'process-stderr': event['stderr'] = 'uncertain'
        else:
            event['stdout'] = (' '.join(f'{i:064x}' for i in range(257)) if failure == 'container-count'
                               else 'a'*64+' '+'a'*64 if failure == 'duplicate-container' else '')
        return event
    with socket.socket(socket.AF_UNIX) as sock:
        target = owned.root/'ii/s'
        if failure == 'socket-symlink':
            sock.bind(str(owned.root/'ii/real-sock')); target.symlink_to(owned.root/'ii/real-sock')
        else: sock.bind(str(target))
        if failure == 'socket-symlink':
            result = observe.capture(owned.root, {}, {'DOCKER_HOST':'unix://'+str(target)}, run=run)
            assert any(row['reason'] == 'unsafe Docker socket' for row in result['unknown_namespace'])
        else:
            with pytest.raises(RuntimeError, match='container count/duplicate bound|process observation failed/bound/uncertain'):
                observe.capture(owned.root, {}, {'DOCKER_HOST':'unix://'+str(target)}, run=run)


def test_socket_activation_original_bounds_not_replaced(original, owned):
    fn = owned.scope['_orn_survey'](original.probe, {'__file__':str(original.kit/'reuse/process_probe.py')})
    ns = fn.__globals__
    original_ns = module(original.probe.encode(), original.kit/'reuse/process_probe.py').__dict__
    for name in ('resolve_activated', 'socket_fds', 'bounded_text', 'unix_listeners', 'docker_hosts'):
        assert ns[name].__code__.co_code == original_ns[name].__code__.co_code
    for raw in ('header\nmalformed', 'header\n0: 0 0 00010000 0001 01 123 @abstract'):
        with pytest.raises(ValueError): ns['unix_listeners'](raw, {123})
    table = owned.base/'table'; table.write_text('12345')
    with pytest.raises(ValueError, match='observation bound'): ns['bounded_text'](table, 4)
    assert ns['docker_hosts'](['dockerd', '-H', 'tcp://127.0.0.1:2375']) == ['tcp://127.0.0.1:2375']
    with pytest.raises(ValueError): ns['docker_hosts'](['dockerd', '--host'])


def test_current_socket_paths_fit_and_overflow_still_rejected(owned, monkeypatch):
    assert len(os.fsencode(owned.endpoint/'extensions/callback.sock')) < 108
    assert owned.scope['_orn_proof'](owned.shared, owned.root)
    encode = os.fsencode
    monkeypatch.setattr(os, 'fsencode', lambda v: b'x'*108 if str(v).endswith('/extensions/callback.sock') else encode(v))
    with pytest.raises(ValueError, match='Unix socket path bound'):
        owned.scope['_orn_proof'](owned.shared, owned.root)


def test_probe_child_really_executes_original_survey_without_cloud(original, tmp_path):
    shared = tmp_path/'shared'; root = shared/'job/task'; root.mkdir(parents=True)
    scope = runtime()
    argv = scope['_orn_probe_command'](original.kit/'reuse/process_probe.py', shared, root)
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ''
    observed = json.loads(completed.stdout)
    assert observed['schema'] == 'reviewbench-node-process-observation/v1'
    assert observed['scanned'] > 0 and observed['owned_runtime_namespace'] is None
    assert isinstance(observed['unknown'], list)  # permissions/foreign processes are NOT suppressed


def test_transform_target_no_fixed_c3_identity_and_all13_inputs(original):
    if not C3.exists(): pytest.skip('all13 original index not available')
    index = json.loads(C3.read_bytes())
    assert len(index['pairs']) == 13
    hashes, roots = set(), set()
    for ref in index['pairs']:
        assert pin(Path(ref['path'])) == ref
        row = json.loads(Path(ref['path']).read_bytes()); node = Path(row['output'])/'node'
        desc = json.loads((node/'DESCRIPTOR.json').read_bytes()); before_desc = copy.deepcopy(desc)
        before = (node/'ADAPTER286.py').read_bytes()
        generic = G.transform('ADAPTER286.py', before, desc)
        modified = T.transform('ADAPTER286.py', generic, desc)
        loaded = module(modified, node/'ADAPTER286.py')
        assert loaded.PLAN_SHA == desc['canonical_plan']['sha256']
        assert loaded._orn_observer
        assert b'original_reused.__globals__[\'reused\']=reused' in modified
        replacement = T.REUSED_SOURCE.replace('   return paths,observe', '   return paths,_orn_observer(observe,kit,canonical_plan)')
        body = modified.decode().split('\n\n# Owned runtime namespace observer copy;')[0]
        assert body.replace(replacement, T.REUSED_SOURCE) == generic.decode()
        image = G.transform('IMAGE286.py', (node/'IMAGE286.py').read_bytes(), desc)
        assert loaded.IMAGE_SHA275 == sha(image) and loaded.IMAGE_BYTES275 == len(image)
        assert (node/'ADAPTER286.py').read_bytes() == before and desc == before_desc
        roots.add(desc['native_root']); hashes.add(sha(modified))
    assert len(hashes) == 13 and len(roots) >= 2
    print('ALL13: generic -> owned namespace transform accepted 13/13; source pins, plan/resource/identity/cleanup and reused seam retained.')


@pytest.mark.parametrize('damage', ['plan-sha', 'adapter-plan', 'template', 'image-prerequisite', 'duplicate-field', 'reapply'])
def test_transform_literal_and_seam_negatives(original, damage):
    desc = copy.deepcopy(original.descriptor)
    raw = (NODE/'ADAPTER286.py').read_bytes()  # already generic-transformed live input
    if damage == 'plan-sha': desc['canonical_plan']['sha256'] = '0'*64
    elif damage == 'adapter-plan': raw = raw.replace(b'PLAN_SHA = ', b'PLAN_SHA = "wrong" # ', 1)
    elif damage == 'template': raw = raw.replace(b'return paths,observe', b'return paths, observe', 1)
    elif damage == 'image-prerequisite': raw = raw.replace(b'IMAGE_BYTES275 = ', b'IMAGE_BYTES275 = 0 # ', 1)
    elif damage == 'duplicate-field': raw += b'PLAN_SHA = "duplicate"\n'
    else: raw = T.transform('ADAPTER286.py', raw, desc)
    with pytest.raises(ValueError): T.transform('ADAPTER286.py', raw, desc)


@pytest.mark.parametrize('name', ['IMAGE286.py', 'MODIFIED_FILE.py', 'TASK286.py', 'DOCKER286.py', 'any.bin'])
def test_other_roles_are_byte_exact_passthrough(name):
    raw = b'\x00\xffopaque'
    assert T.transform(name, raw, {}) is raw


@pytest.mark.parametrize('bound', ['process-count', 'fd-count', 'cmdline'])
def test_original_process_limits_still_hold(original, owned, monkeypatch, bound):
    fn = owned.scope['_orn_survey'](original.probe, {'__file__': str(original.kit/'reuse/process_probe.py')})
    if bound == 'process-count':
        fake_root = SimpleNamespace(iterdir=lambda: (owned.proc/str(i+1000) for i in range(8193)))
        fn.__globals__['Path'] = lambda value: fake_root if str(value) == '/proc' else Path(value)
    else:
        other = proc_entry(owned.proc, 200, ['/usr/bin/foreign'], owned.root)
        fn.__globals__['Path'] = lambda value: owned.proc if str(value) == '/proc' else Path(value)
        if bound == 'cmdline': (other/'cmdline').write_bytes(b'x'*65537)
        else:
            (other/'fd/3').symlink_to(owned.root/'known')
            real_iterdir = Path.iterdir
            monkeypatch.setattr(Path, 'iterdir', lambda path: iter([other/'fd/3']*4097)
                                if path == other/'fd' else real_iterdir(path))
    result = fn(owned.shared, owned.root)
    reasons = [row.get('detail', row.get('reason')) for row in result['unknown']]
    assert {'process-count':'process count bound', 'fd-count':'fd count bound', 'cmdline':'cmdline bound'}[bound] in reasons


def test_runtime_tree_bound_and_source_drift(owned, original, monkeypatch):
    source = owned.base/'process_probe.py'
    source.write_text(original.probe+'\n# changed\n')
    with pytest.raises(ValueError): owned.scope['_orn_source'](source)
    real_scandir = os.scandir
    class ManyEntries:
        def __enter__(self): return iter([SimpleNamespace(name='same-file')]*4097)
        def __exit__(self, *args): pass
    monkeypatch.setattr(os, 'scandir', lambda path: ManyEntries() if Path(path) == owned.endpoint else real_scandir(path))
    with pytest.raises(ValueError, match='runtime entry count bound'):
        owned.scope['_orn_proof'](owned.shared, owned.root)
