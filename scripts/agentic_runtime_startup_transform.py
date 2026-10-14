"""New-carrier startup policy, without overlapping 45/90-second cutoffs.

The image wrapper shares the existing enclosing image-preparation budget. The
proxy inherits the enclosing TASK286 command's deadline instead of imposing a
second 90-second cutoff. Original image/ownership/resource and G/C/J checks stay
unchanged. Old carriers and pinned transforms remain byte-identical.
"""
import ast
import hashlib
import importlib.util
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent
BASE_SHA = '3500ba7acdf494d0d9c5e739b536f726d2ba33debdb87e5b0a8d1dd556f0bd62'


def original():
    path = BASE / 'agentic_runtime_token_transform.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != BASE_SHA:
        raise ValueError('STARTUP_BASE_TRANSFORM_CHANGED')
    spec = importlib.util.spec_from_file_location('_startup_previous_transform', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checked_policy(policy):
    if set(policy) != {'schema', 'image_prepare_timeout_seconds', 'proxy_startup_timeout_seconds'}:
        raise ValueError('STARTUP_POLICY_FIELDS')
    if policy['schema'] != 'reviewbench-infrastructure-startup-policy/v1':
        raise ValueError('STARTUP_POLICY_SCHEMA')
    timeout = policy['image_prepare_timeout_seconds']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('IMAGE_PREPARATION_FINITE_POSITIVE_TIMEOUT')
    proxy = policy['proxy_startup_timeout_seconds']
    if proxy is not None and (type(proxy) not in (int, float) or not math.isfinite(proxy) or proxy <= 0):
        raise ValueError('PROXY_FINITE_POSITIVE_TIMEOUT_OR_INHERIT')
    return dict(policy)


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('STARTUP_SOURCE_SEAM_CHANGED:' + before[:90])
    return text.replace(before, after, 1)


def image_policy(raw, policy):
    text = raw.decode()
    text = replace_once(text, '\ndef need(value, reason):',
        '\nIMAGE_PREPARATION_TIMEOUT_SECONDS = ' + repr(policy['image_prepare_timeout_seconds']) +
        '\n_IMAGE_PREPARATION_DEADLINE = time.monotonic() + IMAGE_PREPARATION_TIMEOUT_SECONDS\n' +
        '\ndef need(value, reason):')
    text = replace_once(text, 'deadline = time.monotonic() + 45', 'deadline = _IMAGE_PREPARATION_DEADLINE')
    text = replace_once(text, "need(0 < deadline - started <= 90, 'invalid original daemon readiness deadline')",
        "need(deadline > started, 'image preparation budget exhausted before Docker readiness')")
    text = text.replace('private Docker API not ready within original startup deadline',
                        'private Docker API not ready within configured image preparation budget')
    text = text.replace("'original_deadline_monotonic': deadline", "'configured_deadline_monotonic': deadline, 'budget_seconds': IMAGE_PREPARATION_TIMEOUT_SECONDS")
    old = "    text = text.replace(before, '        NATIVE_NODE_WAIT_API(paths, daemon, deadline)\\n' + before)"
    new = """    text = text.replace('deadline = time.monotonic() + 90',
                        'deadline = _RB_IMAGE_PREPARATION_DEADLINE', 1)
    module.__dict__['_RB_IMAGE_PREPARATION_DEADLINE'] = _IMAGE_PREPARATION_DEADLINE
""" + old
    text = replace_once(text, old, new)
    text = replace_once(text, 'Readiness-only HEAD, bounded by the ORIGINAL 90s startup deadline.',
        'Readiness-only HEAD within the configured enclosing image-preparation budget.')
    text = text.replace('daemon restart, timeout extension or accepted-step replay here.',
                        'daemon restart or accepted-step replay here.')
    ast.parse(text, feature_version=(3, 8))
    return text.encode()


def apply_policy(name, raw, descriptor, policy):
    """Apply after the previous pinned composition; keep cleanup pins in sync."""
    policy = checked_policy(policy)
    if name == 'IMAGE286.py':
        return image_policy(raw, policy)
    if name == 'ADAPTER286.py':
        previous = original()
        node = previous.load('agentic_diagnostic_node_transform.py').load('agentic_node_transform.py')
        before, _ = node.images(descriptor)
        old_image = previous.transform('IMAGE286.py', before, descriptor)
        new_image = image_policy(old_image, policy)
        text = raw.decode()
        values = {n.targets[0].id: ast.literal_eval(n.value) for n in ast.parse(text).body
                  if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                  and n.targets[0].id in ('IMAGE_SHA275', 'IMAGE_BYTES275')}
        if values != {'IMAGE_SHA275': hashlib.sha256(old_image).hexdigest(), 'IMAGE_BYTES275': len(old_image)}:
            raise ValueError('STARTUP_CLEANUP_PREVIOUS_IMAGE_PIN')
        text = node.assignment(text, 'IMAGE_SHA275', hashlib.sha256(new_image).hexdigest())
        return node.assignment(text, 'IMAGE_BYTES275', len(new_image)).encode()
    if name == 'MODIFIED_FILE.py':
        text = raw.decode()
        # Mirror the policy at the actual enclosing command, not just the child.
        text = replace_once(text, '"--keep-daemon", "--min-free-gib", "80"], env, 2700)',
            '"--keep-daemon", "--min-free-gib", "80"], env, ' + repr(policy['image_prepare_timeout_seconds']) + ')')
        if policy['proxy_startup_timeout_seconds'] is None:
            text = replace_once(text, '            deadline = time.monotonic() + 90\n',
                '            # Startup inherits the existing enclosing task-command deadline.\n')
            text = replace_once(text, 'process.poll() is None and time.monotonic() < deadline, "original sealed gateway startup failed"',
                'process.poll() is None, "original sealed gateway startup failed"')
        else:
            text = replace_once(text, 'deadline = time.monotonic() + 90',
                'deadline = time.monotonic() + ' + repr(policy['proxy_startup_timeout_seconds']))
        ast.parse(text, feature_version=(3, 8))
        return text.encode()
    return raw


def configure(policy):
    policy = checked_policy(policy)
    previous = original()
    def transform(name, raw, descriptor):
        return apply_policy(name, previous.transform(name, raw, descriptor), descriptor, policy)
    return transform
