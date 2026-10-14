"""Task-scoped runtime ownership for future carriers; no global directory veto.

A sibling entry is neither claimed nor traversed. Only the current endpoint is
eligible for the process-reference exception. Original global process/resource
checks still observe other daemons and references; nothing is cleaned or reused
merely because it lives under .exec. Old pinned carriers remain unchanged.
"""
import ast
import hashlib
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
PINS = {
    'agentic_runtime_startup_transform.py': '3432ff754c68e7c153285b6ec5d4c65f8811e8833c9cdbacb83ebb23fe1dba77',
    'agentic_runtime_mode_transform.py': '491eb9a4ef31bfd4e15d2c030b1ce681bebdf04e8c908eac7086fb68e3e86677',
}


def load(name):
    path = BASE / name
    if hashlib.sha256(path.read_bytes()).hexdigest() != PINS[name]:
        raise ValueError('NAMESPACE_SCOPE_BASE_CHANGED:' + name)
    spec = importlib.util.spec_from_file_location('_namespace_scope_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD = '''    # Any sibling, including another perfectly formed reservation, is HOLD.
    with _orn_os.scandir(base) as entries:
        names = []
        for entry in entries:
            names.append(entry.name)
            _orn_need(len(names) <= 1, 'unexpected exec-root sibling')
    _orn_need(names == [endpoint.name], 'unexpected exec-root sibling')'''
NEW = '''    # Ownership is per task, not a claim that the whole node has one task.
    # Do not enumerate, traverse, alter or adopt another task's runtime entries.
    mirror = current/'ii/NATIVE_EXEC_ROOT.json'
    if not mirror.exists() and not mirror.is_symlink():
        # A short-path task may coexist with another task's compact namespace.
        # Keep its original observer contract; a missing own reservation cannot
        # be disguised as short-path mode when its config uses the compact root.
        _orn_need(not endpoint.exists() and not endpoint.is_symlink(),
                  'current runtime reservation missing mirror')
        config = _orn_json_read(_orn_read(current/'ii/daemon.json'))
        _orn_need(config.get('exec-root') == str(current/'x'),
                  'compact current runtime reservation missing')
        return None'''


def runtime_source():
    source = load('agentic_runtime_mode_transform.py').runtime_source()
    if source.count(OLD) != 1:
        raise ValueError('EXACT_GLOBAL_SIBLING_VETO_SEAM')
    source = source.replace(OLD, NEW, 1)
    old = "if proof is not None and job==shared/'.exec':continue"
    if source.count(old) != 1:
        raise ValueError('EXACT_RUNTIME_NAMESPACE_CLASSIFIER_SEAM')
    # _orn_proof checks the namespace parent before returning, also for a
    # short-path current task. The original process survey remains unchanged.
    source = source.replace(old, "if job==shared/'.exec':continue", 1)
    ast.parse(source, feature_version=(3, 8))
    return source


def apply_scope(name, raw, descriptor):
    if name != 'ADAPTER286.py':
        return raw
    text = raw.decode()
    nodes = [n for n in ast.parse(text).body if isinstance(n, ast.Assign)
             and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
             and n.targets[0].id == '_ORN_RUNTIME_SOURCE']
    if len(nodes) != 1 or ast.literal_eval(nodes[0].value) != load('agentic_runtime_mode_transform.py').runtime_source():
        raise ValueError('SCOPE_REQUIRES_PREVIOUS_REVIEWED_NAMESPACE')
    node = nodes[0]
    lines = text.splitlines(True)
    lines[node.lineno - 1:node.end_lineno] = ['_ORN_RUNTIME_SOURCE = ' + repr(runtime_source()) + '\n']
    result = ''.join(lines).encode()
    ast.parse(result, feature_version=(3, 8))
    return result


def configure(policy):
    previous = load('agentic_runtime_startup_transform.py').configure(policy)
    def transform(name, raw, descriptor):
        return apply_scope(name, previous(name, raw, descriptor), descriptor)
    return transform
