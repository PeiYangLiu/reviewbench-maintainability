"""Separate canonical storage identity from physical Batch identity.

Compose after the pinned runtime-scope release. New preparation metadata is v2;
its canonical worker and physical worker are explicit. Old v1 metadata is read
without rewriting it, using its protected plan and preparation receipt to check
mapping consistency. This observation does not authorize cleanup or native work;
original process/resource/lease and native-once checks remain authoritative.
"""
import ast
import hashlib
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
PREVIOUS_SHA = 'e0c585a44cfad976704a45a62f377cbcc1dd7c363711069a42684fd80fcc98ac'


def previous():
    path = BASE / 'agentic_runtime_namespace_scope_transform.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != PREVIOUS_SHA:
        raise ValueError('IDENTITY_PREVIOUS_SCOPE_CHANGED')
    spec = importlib.util.spec_from_file_location('_identity_previous_scope', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDENTITY_SOURCE = r'''
def _orn_legacy_worker(root, value):
    """Observe a stored identity mapping; not a new authorization or cleanup."""
    root = _orn_Path(root)
    schema = value.get('schema')
    if schema not in ('reviewbench-managed-single-pair-preparation-context/v1',
                      'reviewbench-managed-single-pair-preparation-context/v2'):
        # Unchanged contract for unrelated, original legacy supervisor formats.
        _orn_need(value.get('worker') == root.parent.name + '/' + root.name,
                  'legacy worker identity/path disagree')
        return value['worker']
    _orn_dir(root)
    _orn_dir(root/'managed-kit')
    config_path = root/'node-supervisor-config.json'
    plan_path = root/'managed-kit/PLAN.json'
    grant_path = root/'PREPARATION_AUTHORIZATION.original.json'
    config_raw = _orn_read(config_path, maximum=16*1024**2)
    _orn_need(_orn_json_read(config_raw) == value, 'legacy config changed')
    plan_raw = _orn_read(plan_path, maximum=16*1024**2)
    digest = _orn_hashlib.sha256(plan_raw).hexdigest()
    _orn_need(digest == value.get('plan_sha256'), 'legacy plan hash mismatch')
    plan = _orn_json_read(plan_raw)
    mapping = plan['mapping']
    _orn_need(mapping['root'] == str(root) and plan['binding_id'] == value.get('binding_id'),
              'legacy plan/root/binding mismatch')
    _orn_need(value.get('native_authorized') is False, 'legacy preparation context kind')
    grant_raw = _orn_read(grant_path, maximum=16*1024**2)
    packet = _orn_json_read(grant_raw)
    body = packet['body']
    # This is the receipt already checked before the original writer persisted
    # it. We check local consistency, not its expired deadline or a new MAC grant.
    _orn_need(body.get('schema') == 'reviewbench-managed-cohort-preparation-grant/v1'
              and body.get('allow_prepare_only') is True and body.get('allow_native') is False
              and body.get('plan_sha256') == digest and body.get('mapping') == mapping
              and body.get('binding_id') == plan['binding_id'], 'legacy preparation receipt mapping')
    actual = body['actual_node']
    _orn_need(actual['pool_id'] == mapping['pool_id'], 'legacy physical pool mismatch')
    physical = actual['job_id'] + '/' + actual['task_id']
    canonical = mapping['job_id'] + '/' + mapping['task_id']
    if schema.endswith('/v1'):
        _orn_need(value.get('worker') == physical, 'legacy v1 physical worker mismatch')
    else:
        _orn_need(value.get('worker') == canonical and value.get('physical_worker') == physical
                  and value.get('storage_root') == str(root), 'legacy v2 explicit mapping mismatch')
    for path, raw in ((config_path, config_raw), (plan_path, plan_raw), (grant_path, grant_raw)):
        _orn_need(_orn_read(path, maximum=16*1024**2) == raw, 'legacy mapping changed during observation')
    return value['worker']
'''


def runtime_source():
    text = previous().runtime_source()
    seam = '\ndef _orn_observer(observe, kit, plan):'
    if text.count(seam) != 1:
        raise ValueError('IDENTITY_OBSERVER_SEAM')
    text = text.replace(seam, '\n' + IDENTITY_SOURCE + seam, 1)
    seam = '    scope = dict(observe.capture.__globals__)'
    if text.count(seam) != 1:
        raise ValueError('IDENTITY_READER_GLOBALS_SEAM')
    patch = '''    source = _orn_replace(source,
        "need(worker==job.name+'/'+root.name,'legacy worker identity/path disagree')",
        "worker=_orn_legacy_worker(root,value)")
'''
    text = text.replace(seam, patch + seam + "\n    scope['_orn_legacy_worker'] = _orn_legacy_worker", 1)
    ast.parse(text, feature_version=(3, 8))
    return text


OLD_WRITER = '''        "schema": "reviewbench-managed-single-pair-preparation-context/v1",
        "worker": actual["job_id"] + "/" + actual["task_id"], "binding_id": plan["binding_id"],'''
NEW_WRITER = '''        "schema": "reviewbench-managed-single-pair-preparation-context/v2",
        "worker": plan["mapping"]["job_id"] + "/" + plan["mapping"]["task_id"],
        "physical_worker": actual["job_id"] + "/" + actual["task_id"],
        "storage_root": str(root), "binding_id": plan["binding_id"],'''


def apply_identity(name, raw, descriptor):
    if name == 'MODIFIED_FILE.py':
        text = raw.decode()
        if text.count(OLD_WRITER) != 1:
            raise ValueError('IDENTITY_EXACT_PREVIOUS_WRITER')
        result = text.replace(OLD_WRITER, NEW_WRITER, 1).encode()
    elif name == 'ADAPTER286.py':
        text = raw.decode()
        nodes = [n for n in ast.parse(text).body if isinstance(n, ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == '_ORN_RUNTIME_SOURCE']
        if len(nodes) != 1 or ast.literal_eval(nodes[0].value) != previous().runtime_source():
            raise ValueError('IDENTITY_EXACT_PREVIOUS_READER')
        node = nodes[0]
        lines = text.splitlines(True)
        lines[node.lineno - 1:node.end_lineno] = ['_ORN_RUNTIME_SOURCE = ' + repr(runtime_source()) + '\n']
        result = ''.join(lines).encode()
    else:
        return raw
    ast.parse(result, feature_version=(3, 8))
    return result


def configure(policy):
    transform_previous = previous().configure(policy)
    def transform(name, raw, descriptor):
        return apply_identity(name, transform_previous(name, raw, descriptor), descriptor)
    return transform
