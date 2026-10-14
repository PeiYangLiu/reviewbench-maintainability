"""Owned .exec namespace seam for NEW carriers, after agentic_node_transform.

Public: transform(filename, raw, descriptor) -> bytes. Only ADAPTER286.py changes;
no original kit, process probe, historical hook, carrier or cloud is written.
The adapter embeds a process-local observer copy and a bounded original-probe
child. There is no broad .exec allowlist: only the current root's paired, private
reservation and live dockerd identity can explain that one runtime reference.
Unexpected siblings or references by any other PID remain HOLD, not cleanup.
"""
from __future__ import annotations

import ast
import hashlib
import json
from types import ModuleType
from pathlib import Path

OBSERVER_SHA256 = 'f4483d45b16a60218e3cfee52b8a50a2eb7f6b65b9270b0dea53f9ca9e22ee96'
PROCESS_SHA256 = 'a824c0469549ab954b268973ec8a16e1b06b60dc647aaf257974100984407fe6'

# Self-contained on the node and in the ORIGINAL process observer child. Keep
# imports/names private; the outer adapter's trust/identity checks are unchanged.
RUNTIME_SOURCE = r'''
import ast as _orn_ast
import hashlib as _orn_hashlib
import json as _orn_json
import os as _orn_os
import stat as _orn_stat
import sys as _orn_sys
import types as _orn_types
from pathlib import Path as _orn_Path

_ORN_PINS = {
    'node_observe.py': ('f4483d45b16a60218e3cfee52b8a50a2eb7f6b65b9270b0dea53f9ca9e22ee96', 8243),
    'process_probe.py': ('a824c0469549ab954b268973ec8a16e1b06b60dc647aaf257974100984407fe6', 10459),
}
_ORN_PROC = _orn_Path('/proc')


def _orn_need(ok, reason):
    if not ok:
        raise ValueError('OWNED_RUNTIME_NAMESPACE:' + reason)


def _orn_stamp(st):
    return (st.st_dev, st.st_ino, st.st_uid, st.st_mode, st.st_nlink,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _orn_read(path, maximum=65536, private=True):
    path = _orn_Path(path)
    _orn_need(path.is_absolute() and path.resolve() == path, 'file alias')
    before = path.lstat()
    _orn_need(_orn_stat.S_ISREG(before.st_mode) and before.st_nlink == 1
              and before.st_size <= maximum, 'bounded single-link regular file')
    if private:
        _orn_need(before.st_uid == _orn_os.geteuid() and not before.st_mode & 0o022,
                  'file ownership/mode')
    fd = _orn_os.open(path, _orn_os.O_RDONLY | _orn_os.O_NOFOLLOW | _orn_os.O_CLOEXEC)
    with _orn_os.fdopen(fd, 'rb') as stream:
        first = _orn_os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        last = _orn_os.fstat(stream.fileno())
    _orn_need(_orn_stamp(before) == _orn_stamp(first) == _orn_stamp(last)
              == _orn_stamp(path.lstat()) and len(raw) == before.st_size,
              'file changed while reading')
    return raw


def _orn_json_read(raw):
    def unique(rows):
        value = {}
        for key, item in rows:
            _orn_need(key not in value, 'duplicate JSON key')
            value[key] = item
        return value
    return _orn_json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda value: _orn_need(False, 'nonfinite JSON'))


def _orn_source(path):
    path = _orn_Path(path)
    digest, size = _ORN_PINS[path.name]
    raw = _orn_read(path, maximum=size, private=False)
    _orn_need(len(raw) == size and _orn_hashlib.sha256(raw).hexdigest() == digest,
              'original source pin drift:' + path.name)
    return raw.decode('utf-8')


def _orn_dir(path, device=None, private=False):
    path = _orn_Path(path)
    _orn_need(path.is_absolute() and path.resolve() == path, 'directory alias')
    st = path.lstat()
    _orn_need(_orn_stat.S_ISDIR(st.st_mode) and st.st_uid == _orn_os.geteuid()
              and not st.st_mode & (0o077 if private else 0o022)
              and (device is None or st.st_dev == device)
              and not _orn_os.path.ismount(path), 'directory ownership/mode/device')
    return [st.st_dev, st.st_ino, st.st_uid, st.st_mode]


def _orn_tree(endpoint, device):
    # Docker's own internal filenames are version-specific; their ownership is
    # not. No symlink, cross-device entry, foreign UID, writable directory or
    # hard-linked file gets hidden underneath an accepted reservation.
    pending = [endpoint]
    seen = []
    while pending:
        path = pending.pop()
        st = path.lstat()
        _orn_need(len(seen) < 4096, 'runtime entry count bound')
        _orn_need(st.st_uid == _orn_os.geteuid() and st.st_dev == device
                  and not st.st_mode & 0o022, 'runtime ownership/mode/device')
        kind = _orn_stat.S_IFMT(st.st_mode)
        _orn_need(kind in (_orn_stat.S_IFDIR, _orn_stat.S_IFREG, _orn_stat.S_IFSOCK),
                  'runtime symlink/special object')
        _orn_need(kind != _orn_stat.S_IFREG or st.st_nlink == 1, 'runtime hardlink')
        seen.append((path, _orn_stamp(st)))
        if kind == _orn_stat.S_IFDIR:
            # Bound enumeration itself, not only the later walk.
            with _orn_os.scandir(path) as entries:
                for entry in entries:
                    _orn_need(len(seen) + len(pending) < 4096, 'runtime entry count bound')
                    pending.append(path / entry.name)
    for path, stamp in seen:
        _orn_need(_orn_stamp(path.lstat()) == stamp, 'runtime tree changed')


def _orn_proc_bytes(path, maximum):
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    _orn_need(len(raw) <= maximum, 'process observation bound')
    return raw


def _orn_daemon(owner, root):
    pid = owner.get('pid')
    _orn_need(type(pid) is int and pid > 1 and owner.get('root') == str(root),
              'daemon owner/root')
    command = owner.get('command')
    _orn_need(isinstance(command, list) and len(command) == 3
              and command[0] in ('/usr/bin/dockerd', '/usr/sbin/dockerd')
              and command[1:] == ['--config-file', str(root / 'ii/daemon.json')],
              'daemon command')
    expected_paths = dict(base=root/'ii', data=root/'ii/d', exec=root/'x',
                          socket=root/'ii/s', pid=root/'ii/p', config=root/'ii/daemon.json')
    _orn_need(owner.get('paths') == {key: str(value) for key, value in expected_paths.items()},
              'original daemon paths')
    entry = _ORN_PROC / str(pid)
    _orn_need(entry.stat().st_uid == _orn_os.geteuid(), 'daemon UID')
    before = _orn_proc_bytes(entry/'stat', 8192).decode()
    fields = before[before.rfind(')') + 2:].split()
    start = fields[19]
    _orn_need(start == str(owner.get('proc_start_ticks')) and fields[0] not in ('Z', 'X')
              and int(fields[2]) == pid, 'daemon PID/start/process group')
    parts = [_orn_os.fsdecode(x) for x in _orn_proc_bytes(entry/'cmdline', 65536).split(b'\0') if x]
    _orn_need(parts == command, 'live daemon argv')
    _orn_need((entry/'exe').resolve() == _orn_Path(command[0]).resolve(), 'daemon executable')
    for name in ('mnt', 'net'):
        _orn_need(_orn_os.readlink(entry/'ns'/name) == _orn_os.readlink(_ORN_PROC/'self/ns'/name),
                  'daemon namespace:' + name)
    after = _orn_proc_bytes(entry/'stat', 8192).decode()
    later = after[after.rfind(')') + 2:].split()
    _orn_need(later[19] == start and later[0] not in ('Z', 'X') and int(later[2]) == pid,
              'daemon identity changed')
    return dict(pid=pid, process_start=start, command=command)


def _orn_proof(shared, current):
    shared, current = _orn_Path(shared), _orn_Path(current)
    _orn_need(current.parent.parent == shared, 'current/shared binding')
    base = shared / '.exec'
    if not base.exists() and not base.is_symlink():
        # The ordinary short-path case uses the wholly original observer.
        _orn_need(not (current/'ii/NATIVE_EXEC_ROOT.json').exists()
                  and not (current/'ii/NATIVE_EXEC_ROOT.json').is_symlink(), 'reservation vanished')
        return None
    directory_ids = [ _orn_dir(shared) ]
    device = directory_ids[0][0]
    for path in (current.parent, current, current/'ii'):
        directory_ids.append(_orn_dir(path, device))
    directory_ids.append(_orn_dir(base, device))
    endpoint = base / _orn_hashlib.sha256(_orn_os.fsencode(current)).hexdigest()[:32]
    # Any sibling, including another perfectly formed reservation, is HOLD.
    with _orn_os.scandir(base) as entries:
        names = []
        for entry in entries:
            names.append(entry.name)
            _orn_need(len(names) <= 1, 'unexpected exec-root sibling')
    _orn_need(names == [endpoint.name], 'unexpected exec-root sibling')
    directory_ids.append(_orn_dir(endpoint, device, private=True))
    _orn_tree(endpoint, device)
    expected = {'schema': 'reviewbench-native-exec-root/v1', 'root': str(current),
                'exec_root': str(endpoint), 'original_exec_root': str(current/'x'),
                'native_authorized': False, 'automatic_retry': False}
    expected_raw = (_orn_json.dumps(expected, sort_keys=True) + '\n').encode()
    paths = (endpoint/'OWNER.json', current/'ii/NATIVE_EXEC_ROOT.json',
             current/'ii/daemon.json', current/'ii/OWNED_DAEMON.json', current/'ii/p')
    raws = [_orn_read(path) for path in paths]
    _orn_need(raws[0] == raws[1] == expected_raw, 'paired exact reservation records')
    config, owner = _orn_json_read(raws[2]), _orn_json_read(raws[3])
    _orn_need(config.get('exec-root') == str(endpoint)
              and config.get('data-root') == str(current/'ii/d')
              and config.get('pidfile') == str(current/'ii/p')
              and config.get('hosts') == ['unix://' + str(current/'ii/s')], 'daemon config binding')
    # The existing image helper owns containerd separately under the current
    # root. This alias never expands its state/data/socket ownership domain.
    managed = current/'x/containerd/containerd.sock'
    endpoints = [str(managed), str(current/'x/containerd/containerd-debug.sock'), str(managed)+'.ttrpc']
    containerd = str(managed) if all(len(_orn_os.fsencode(x)) < 108 for x in endpoints) else str(current/'ii/c')
    _orn_need(config.get('containerd') == containerd, 'foreign containerd')
    _orn_need(all(len(_orn_os.fsencode(x)) < 108 for x in (
        str(endpoint/'extensions/callback.sock'), str(current/'ii/s'), containerd,
        containerd+'.ttrpc', str(current/'ii/cd') if containerd == str(current/'ii/c') else endpoints[1])),
        'Unix socket path bound')
    daemon = _orn_daemon(owner, current)
    _orn_need(raws[4].strip() == str(daemon['pid']).encode(), 'daemon pidfile')
    _orn_need(all(_orn_read(path) == raw for path, raw in zip(paths, raws)), 'ownership changed')
    return dict(schema='reviewbench-owned-runtime-namespace/v1', root=str(current),
                exec_root=str(endpoint), directory_identities=directory_ids,
                records_sha256=[_orn_hashlib.sha256(raw).hexdigest() for raw in raws], **daemon)


def _orn_current_reference(candidate, entry, start, parts, proof):
    if proof is None or candidate != proof['exec_root']:
        return False
    _orn_need(int(entry.name) == proof['pid'] and start == proof['process_start']
              and parts == proof['command'], 'unowned process references current exec-root')
    return True


def _orn_replace(source, old, new):
    _orn_need(source.count(old) == 1, 'original source seam drift')
    return source.replace(old, new)


def _orn_survey(source, namespace):
    # Preserve every original /proc bound, vanished/unknown classification,
    # legacy-root reference, Docker endpoint and socket-activation check.
    source = _orn_replace(source, 'def survey(shared,current):\n',
                          'def survey(shared,current):\n proof=_orn_proof(shared,current)\n')
    old = "if candidate!=str(current) and match[1] not in {'node-global-gate','node-shared-scratch-v1','object-cache','scratch'}:refs.add(candidate)"
    source = _orn_replace(source, old, old.replace(':refs.add(candidate)',
        ' and not _orn_current_reference(candidate,entry,start,parts,proof):refs.add(candidate)'))
    # survey()'s sole return is deliberately anchored after its original catch.
    source = _orn_replace(source, " return result\nif __name__=='__main__':",
        " _orn_need(_orn_proof(shared,current)==proof,'process ownership changed')\n"
        " result['owned_runtime_namespace']=proof\n return result\nif __name__=='__main__':")
    scope = dict(namespace, __name__='_owned_runtime_probe')
    scope.update(_orn_proof=_orn_proof, _orn_need=_orn_need,
                 _orn_current_reference=_orn_current_reference)
    exec(compile(source, scope['__file__'] + ':owned-runtime-copy', 'exec'), scope)
    return scope['survey']


def _orn_probe_cli():
    _orn_need(len(_orn_sys.argv) == 4, 'probe arguments')
    path, shared, current = map(_orn_Path, _orn_sys.argv[1:])
    source = _orn_source(path)
    survey = _orn_survey(source, {'__file__': str(path)})
    print(_orn_json.dumps(survey(shared, current), sort_keys=True))


def _orn_probe_command(path, shared, current):
    _orn_source(path)  # Recheck before starting; child checks independently.
    return [_orn_sys.executable, '-B', '-c', _ORN_RUNTIME_SOURCE + '\n_orn_probe_cli()\n',
            str(path), str(shared), str(current)]


def _orn_observer(observe, kit, plan):
    kit = _orn_Path(kit)
    for name, (digest, size) in _ORN_PINS.items():
        ref = plan['files']['reuse/' + name]
        _orn_need(ref['sha256'] == digest and ref['bytes'] == size, 'plan observer pin drift')
        _orn_source(kit/'reuse'/name)
    _orn_need(_orn_Path(observe.__file__).resolve() == (kit/'reuse/node_observe.py').resolve(),
              'foreign observer module')
    source = _orn_source(kit/'reuse/node_observe.py')
    source = _orn_replace(source, 'def legacy_roots(shared,current):\n',
        "def legacy_roots(shared,current):\n proof=_orn_proof(shared,current)\n")
    source = _orn_replace(source, ' for job in sorted(shared.iterdir()):\n',
        " for job in sorted(shared.iterdir()):\n  if proof is not None and job==shared/'.exec':continue\n")
    old = "[sys.executable,'-B',str(Path(__file__).with_name('process_probe.py')),'--shared-root',str(shared),'--current-root',str(root)]"
    source = _orn_replace(source, old,
        "_orn_probe_command(Path(__file__).with_name('process_probe.py'),shared,root)")
    # Recompile ORIGINAL function definitions in a private globals copy, never
    # hot-edit the kit or mutate the imported observer's shared module globals.
    scope = dict(observe.capture.__globals__)
    scope.update(_orn_proof=_orn_proof, _orn_probe_command=_orn_probe_command)
    nodes = [node for node in _orn_ast.parse(source).body if isinstance(node, _orn_ast.FunctionDef)]
    exec(compile(_orn_ast.Module(body=nodes, type_ignores=[]),
                 str(kit/'reuse/node_observe.py') + ':owned-runtime-copy', 'exec'), scope)
    scope['identity'] = observe.identity  # retain descriptor-bound physical seam
    capture, critical = scope['capture'], scope['critical']
    def bound_capture(root, config, env, *, run=scope['command']):
        _orn_need(str(root) == plan['mapping']['root'], 'observer current root drift')
        current = _orn_Path(root)
        before = _orn_proof(current.parent.parent, current)
        result = capture(root, config, env, run=run)
        _orn_need(result['processes'].get('owned_runtime_namespace') == before
                  and _orn_proof(current.parent.parent, current) == before, 'capture ownership changed')
        result['owned_runtime_namespace'] = before
        return result
    def bound_critical(observation):
        return {**critical(observation), 'owned_runtime_namespace': observation['owned_runtime_namespace']}
    scope.update(capture=bound_capture, critical=bound_critical)
    return _orn_types.SimpleNamespace(**scope)
'''

# Exact existing physical-placement closure, excluding its indentation. All
# other adapter text is retained, including identity/grant/resource/cleanup pins.
REUSED_SOURCE = '''def reused(kit,canonical_plan):
   bind_plan(d,canonical_plan);paths,observe=original_reused(kit,canonical_plan)
   # capture() star-imports identity into its OWN module globals. Adapt that
   # exact consumer, not an unused producer and not the process environment.
   for member in ('reuse/node_observe.py','reuse/integration_common.py'):
    ref=canonical_plan['files'][member];checked_file(Path(kit)/member,ref['sha256'],ref['bytes'])
   need(Path(observe.__file__).resolve()==(Path(kit)/'reuse/node_observe.py').resolve(),'foreign observer270')
   source=(Path(kit)/'reuse/integration_common.py').read_text();tree=ast.parse(source)
   f=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='identity')
   text=ast.get_source_segment(source,f)
   old="root.parent.name==d['job_id'] and root.name==d['task_id']"
   need(text.count(old)==1,'exact observer identity seam drift270')
   text=text.replace(old,"str(root)==RECOVERY_ROOT270 and {k:d[k] for k in ('job_id','task_id','pool_id')}==RECOVERY_PLACEMENT270")
   ns=dict(observe.identity.__globals__,RECOVERY_ROOT270=d['native_root'],RECOVERY_PLACEMENT270=dict(d['physical_mapping']))
   exec(compile(text,str(Path(kit)/'reuse/integration_common.py')+':descriptor-bound270','exec'),ns)
   observe.identity=ns['identity']
   return paths,observe'''


def transform(filename: str, raw: bytes, descriptor: dict) -> bytes:
    """Append to the generic hook; reject drift instead of guessing a seam."""
    if filename != 'ADAPTER286.py':
        return raw
    ref = descriptor['canonical_plan']
    # Reuse the same bounded/nonsymlink reader as the generated runtime.
    helpers = {}
    exec(compile(RUNTIME_SOURCE, __file__ + ':helpers', 'exec'), helpers)
    plan_raw = helpers['_orn_read'](ref['path'], maximum=16*1024**2, private=False)
    if len(plan_raw) != ref['bytes'] or hashlib.sha256(plan_raw).hexdigest() != ref['sha256']:
        raise ValueError('CANONICAL_PLAN_PIN_DRIFT')
    plan = helpers['_orn_json_read'](plan_raw)
    if descriptor['native_root'] != descriptor['canonical']['mapping']['root'] or plan['mapping'] != descriptor['canonical']['mapping']:
        raise ValueError('CANONICAL_ROOT_BINDING_DRIFT')
    root = Path(plan['mapping']['root'])
    if not root.is_absolute() or root.parts[:4] != ('/', 'var', 'lib', 'reviewbench399') or len(root.parts) != 6:
        raise ValueError('CANONICAL_TASK_ROOT_REQUIRED')
    for name, (digest, size) in helpers['_ORN_PINS'].items():
        pin = plan['files']['reuse/' + name]
        if pin['sha256'] != digest or pin['bytes'] != size:
            raise ValueError('ORIGINAL_OBSERVER_TEMPLATE_DRIFT:' + name)
        helpers['_orn_source'](Path(ref['path']).parent/'reuse'/name)
    text = raw.decode('utf-8')
    tree = ast.parse(text)
    if '_ORN_RUNTIME_SOURCE' in text:
        raise ValueError('OWNED_RUNTIME_TRANSFORM_ALREADY_APPLIED')
    def literal(name):
        found = [node.value for node in tree.body if isinstance(node, ast.Assign)
                 and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id == name]
        if len(found) != 1:
            raise ValueError('EXACT_ADAPTER_LITERAL_REQUIRED:' + name)
        return ast.literal_eval(found[0])
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if literal('PLAN_SHA') != ref['sha256'] or literal('PLAN_DIGEST') != hashlib.sha256(canonical).hexdigest():
        raise ValueError('ADAPTER_PLAN_PIN_DRIFT')
    seams = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'reused']
    if len(seams) != 1 or ast.get_source_segment(text, seams[0]) != REUSED_SOURCE:
        raise ValueError('EXACT_ADAPTER_REUSED_TEMPLATE_REQUIRED')
    # Make the generic-image prerequisite explicit without modifying that hook.
    generic_path = Path(__file__).with_name('agentic_node_transform.py')
    generic_raw = generic_path.read_bytes()
    if hashlib.sha256(generic_raw).hexdigest() != 'c20404553bc0bf125b0033dc7495da9aebf884fae38dc8cb63cc1488ccf241de':
        raise ValueError('HISTORICAL_GENERIC_TRANSFORM_PIN_DRIFT')
    generic = ModuleType('_owned_namespace_generic_input')
    generic.__file__ = str(generic_path)
    exec(compile(generic_raw, str(generic_path), 'exec'), generic.__dict__)
    _, image = generic.images(descriptor)
    if literal('IMAGE_SHA275') != hashlib.sha256(image).hexdigest() or literal('IMAGE_BYTES275') != len(image):
        raise ValueError('RUN_GENERIC_NODE_TRANSFORM_FIRST')
    replacement = REUSED_SOURCE.replace('   return paths,observe',
        '   return paths,_orn_observer(observe,kit,canonical_plan)')
    if text.count(REUSED_SOURCE) != 1:
        raise ValueError('AMBIGUOUS_ADAPTER_REUSED_TEMPLATE')
    text = text.replace(REUSED_SOURCE, replacement)
    text += '\n\n# Owned runtime namespace observer copy; no frozen source writes.\n'
    text += '_ORN_RUNTIME_SOURCE = ' + repr(RUNTIME_SOURCE) + '\n'
    text += "exec(compile(_ORN_RUNTIME_SOURCE, __file__ + ':owned-runtime-namespace', 'exec'))\n"
    result = text.encode('utf-8')
    ast.parse(result)
    return result
