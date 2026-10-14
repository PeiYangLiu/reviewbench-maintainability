"""Post-diagnostic ADAPTER286-only overlay; no frozen-source or cloud writes.

Public integration: transform(filename, raw, descriptor) -> bytes. Compose AFTER
agentic_diagnostic_node_transform.transform. The delivered adapter embeds all
runtime code; neither this script nor third-party packages are needed on-node.
"""
import ast
import hashlib
import json
from pathlib import Path

PHYSICAL_SHA256 = '86efaaea18144ac8c54871fea6c1dfadc5824ff7a29a97474e3c6682eea3b78b'
PHYSICAL_BYTES = 28346

RUNTIME_SOURCE = r'''
import ast as _imds_ast
import hashlib as _imds_hashlib
import math as _imds_math
import os as _imds_os
import stat as _imds_stat
import threading as _imds_threading
import time as _imds_time
import urllib.error as _imds_error
import urllib.parse as _imds_parse
import urllib.request as _imds_request
from email.utils import parsedate_to_datetime as _imds_http_date
from pathlib import Path as _imds_Path

_IMDS_PHYSICAL_SHA = '86efaaea18144ac8c54871fea6c1dfadc5824ff7a29a97474e3c6682eea3b78b'
_IMDS_PHYSICAL_BYTES = 28346
_IMDS_ENDPOINT = 'http://169.254.169.254/metadata/identity/oauth2/token'
_IMDS_RESOURCE = 'https://storage.azure.com/'
_IMDS_MARGIN = 60.0
_IMDS_ATTEMPTS = 4
_IMDS_DELAY_CAP = 5.0
_IMDS_SLEEP_CAP = 15.0
_IMDS_MAX_ENTRIES = 128


def _imds_need(ok, reason):
    if not ok:
        # Never include response bodies, tokens, or caller payloads in errors.
        raise ValueError('IMDS_TOKEN_CACHE: ' + reason)


def _imds_reset():
    global _IMDS_STATE
    # Replacing the inherited lock is essential if another thread held it at
    # fork. A PID in the key alone would still deadlock on that inherited lock.
    _IMDS_STATE = (_imds_os.getpid(), _imds_threading.Lock(), {})


_imds_reset()
if hasattr(_imds_os, 'register_at_fork'):
    _imds_os.register_at_fork(after_in_child=_imds_reset)


def _imds_number(value):
    _imds_need(type(value) in (str, int, float), 'invalid expiry')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError('IMDS_TOKEN_CACHE: invalid expiry') from None
    _imds_need(_imds_math.isfinite(number) and number > 0, 'invalid expiry')
    return number


def _imds_delay(error, attempt):
    value = error.headers.get('Retry-After') if error.headers is not None else None
    delay = 0.5 * (2 ** attempt)
    if value is not None:
        try:
            seconds = float(value)
        except (ValueError, TypeError, OverflowError):
            try:
                instant = _imds_http_date(value)
                if instant.tzinfo is None:
                    return None
                seconds = max(0.0, instant.timestamp() - _imds_time.time())
            except (ValueError, TypeError, OverflowError, AttributeError):
                return None
        if not _imds_math.isfinite(seconds) or seconds < 0:
            return None
        delay = max(delay, seconds)
    # Do NOT truncate Retry-After and send early. A server delay outside our
    # bound, or an unparseable hint, propagates the original HTTP429 instead.
    return delay if delay <= _IMDS_DELAY_CAP else None


def _imds_token(opener, client_id, resource, endpoint, decode):
    global _IMDS_STATE
    pid = _imds_os.getpid()
    if _IMDS_STATE[0] != pid:
        _imds_reset()
    _, lock, cache = _IMDS_STATE
    key = (pid, client_id, resource, endpoint)
    # Single-flight across callers, including different physical() instances.
    # Only the metadata request is inside this lock/retry scope, never a PUT.
    with lock:
        now, mono = _imds_time.time(), _imds_time.monotonic()
        for old, (_, wall_deadline, mono_deadline) in list(cache.items()):
            if now >= wall_deadline or mono >= mono_deadline:
                del cache[old]
        if key in cache:
            return cache[key][0]
        query = _imds_parse.urlencode({'api-version': '2018-02-01',
                                     'resource': resource, 'client_id': client_id})
        request = _imds_request.Request(endpoint + '?' + query, headers={'Metadata': 'true'})
        slept = 0.0
        for attempt in range(_IMDS_ATTEMPTS):
            started = _imds_time.monotonic()
            try:
                response = opener.open(request, timeout=15)
            except _imds_error.HTTPError as error:
                if error.code != 429 or attempt + 1 == _IMDS_ATTEMPTS:
                    raise
                delay = _imds_delay(error, attempt)
                if delay is None or slept + delay > _IMDS_SLEEP_CAP:
                    raise
                error.close()
                _imds_time.sleep(delay)
                slept += delay
                continue
            # No retries for read/parse failures or any other HTTP/transport
            # error. Original no-proxy, no-redirect opener comes from put_blob.
            with response:
                raw = response.read(131073)
            _imds_need(len(raw) <= 131072, 'metadata response size bound')
            try:
                record = decode(raw)
            except (ValueError, TypeError, UnicodeError):
                raise ValueError('IMDS_TOKEN_CACHE: invalid metadata JSON') from None
            _imds_need(isinstance(record, dict), 'invalid metadata object')
            token = record.get('access_token')
            _imds_need(isinstance(token, str) and bool(token)
                       and all(33 <= ord(c) <= 126 for c in token), 'invalid access token')
            now, mono = _imds_time.time(), _imds_time.monotonic()
            remaining = []
            if 'expires_on' in record:
                remaining.append(_imds_number(record['expires_on']) - now)
            if 'expires_in' in record:
                # Anchor relative expiry BEFORE the successful request; network
                # latency must not manufacture additional token lifetime.
                remaining.append(_imds_number(record['expires_in']) - (mono - started))
            _imds_need(bool(remaining), 'actual token expiry required')
            lifetime = min(remaining)
            _imds_need(lifetime > 0, 'expired metadata token')
            usable = lifetime - _IMDS_MARGIN
            if usable > 0:
                if len(cache) >= _IMDS_MAX_ENTRIES:
                    del cache[next(iter(cache))]
                # Both clocks must remain inside validity: backwards wall-clock
                # steps cannot extend lifetime; forwards steps expire promptly.
                cache[key] = (token, now + usable, mono + usable)
            return token
        raise AssertionError('IMDS_TOKEN_CACHE: unreachable attempt bound')


_IMDS_TOKEN_BLOCK = ''' + repr('''    query = urllib.parse.urlencode({"api-version": "2018-02-01", "resource": "https://storage.azure.com/",
                                    "client_id": C.MI_CLIENT})
    request = urllib.request.Request("http://169.254.169.254/metadata/identity/oauth2/token?" + query,
                                     headers={"Metadata": "true"})
    with opener.open(request, timeout=15) as response:
        token = C.decode(response.read(131073))["access_token"]
''') + r'''
_IMDS_TOKEN_CALL = '    token = _imds_token(opener, C.MI_CLIENT, _IMDS_RESOURCE, _IMDS_ENDPOINT, C.decode)\n'


def _imds_source(kit, plan):
    ref = plan['files']['reuse/physical_node.py']
    _imds_need(ref['sha256'] == _IMDS_PHYSICAL_SHA
               and ref['bytes'] == _IMDS_PHYSICAL_BYTES, 'canonical physical pin drift')
    path = _imds_Path(kit) / 'reuse/physical_node.py'
    _imds_need(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
               'canonical physical path required')
    before = path.stat()
    _imds_need(_imds_stat.S_ISREG(before.st_mode) and before.st_nlink == 1
               and before.st_size == _IMDS_PHYSICAL_BYTES, 'physical file type/size drift')
    raw = path.read_bytes()
    after = path.stat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink')
    _imds_need(all(getattr(before, k) == getattr(after, k) for k in fields)
               and len(raw) == _IMDS_PHYSICAL_BYTES
               and _imds_hashlib.sha256(raw).hexdigest() == _IMDS_PHYSICAL_SHA,
               'original physical source drift')
    return path, raw.decode('utf-8')


def _imds_install(physical, kit, plan):
    path, source = _imds_source(kit, plan)
    original = physical.put_blob
    shared = original.__globals__
    _imds_need(shared.get('put_blob') is original and original.__closure__ is None
               and original.__defaults__ is None and original.__kwdefaults__ is None,
               'original put_blob globals drift')
    nodes = [n for n in _imds_ast.parse(source).body
             if isinstance(n, _imds_ast.FunctionDef) and n.name == 'put_blob']
    _imds_need(len(nodes) == 1, 'original put_blob definition drift')
    pristine = {}
    exec(compile(_imds_ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), pristine)
    # R.physical AST-loads the original function into a private namespace.
    # Verify the actual callable, not only the disk file, before replacing it.
    attrs = ('co_code', 'co_consts', 'co_names', 'co_varnames', 'co_freevars',
             'co_cellvars', 'co_argcount', 'co_kwonlyargcount', 'co_posonlyargcount')
    _imds_need(all(getattr(original.__code__, k) == getattr(pristine['put_blob'].__code__, k)
                   for k in attrs) and original.__code__.co_filename == str(path),
               'loaded put_blob code drift')
    text = _imds_ast.get_source_segment(source, nodes[0])
    _imds_need(text.count(_IMDS_TOKEN_BLOCK) == 1, 'original token block drift')
    text = text.replace(_IMDS_TOKEN_BLOCK, _IMDS_TOKEN_CALL, 1)
    scope = dict(shared, _imds_token=_imds_token,
                 _IMDS_RESOURCE=_IMDS_RESOURCE, _IMDS_ENDPOINT=_IMDS_ENDPOINT)
    exec(compile(text, str(path) + ':imds-token-cache-copy', 'exec'), scope)
    adapted = scope['put_blob']
    # Both sites matter: create_once/other original functions resolve put_blob
    # through shared globals, whereas carrier callers use physical.put_blob.
    shared['put_blob'] = adapted
    physical.put_blob = adapted
    return physical
'''


def transform(filename, raw, descriptor):
    """Change only ADAPTER286.py, AFTER the pinned diagnostic transform."""
    if filename != 'ADAPTER286.py':
        return raw
    text = raw.decode('utf-8')
    if '_IMDS_RUNTIME_SOURCE' in text:
        raise ValueError('IMDS_TOKEN_CACHE: overlay already installed')
    tree = ast.parse(text)
    originals = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'ORIGINAL' for t in n.targets)]
    if (len(originals) != 1 or ast.literal_eval(originals[0].value)['physical_node.py']
            != {'sha256': PHYSICAL_SHA256, 'bytes': PHYSICAL_BYTES}):
        raise ValueError('IMDS_TOKEN_CACHE: adapter physical source pin drift')
    ref = descriptor['canonical_plan']
    plan_raw = Path(ref['path']).read_bytes()
    if len(plan_raw) != ref['bytes'] or hashlib.sha256(plan_raw).hexdigest() != ref['sha256']:
        raise ValueError('IMDS_TOKEN_CACHE: canonical plan pin drift')
    physical_ref = json.loads(plan_raw)['files']['reuse/physical_node.py']
    if physical_ref != {'sha256': PHYSICAL_SHA256, 'bytes': PHYSICAL_BYTES}:
        raise ValueError('IMDS_TOKEN_CACHE: canonical physical pin drift')
    seam = '  bind_plan(d,plan);physical=original_physical(kit,plan);original_actual=physical.actual_node\n'
    if text.count(seam) != 1:
        raise ValueError('IMDS_TOKEN_CACHE: install.physical closure seam drift')
    text = text.replace(seam, seam + '  _imds_install(physical,kit,plan)\n', 1)
    text += ('\n# Process-local IMDS cache; original PUT/claims/credentials are unchanged.\n'
             '_IMDS_RUNTIME_SOURCE = ' + repr(RUNTIME_SOURCE) + '\n'
             "exec(compile(_IMDS_RUNTIME_SOURCE, __file__ + ':imds-token-cache', 'exec'))\n")
    compile(text, filename, 'exec')
    return text.encode('utf-8')
