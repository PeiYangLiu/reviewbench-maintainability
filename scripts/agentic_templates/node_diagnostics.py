"""Injected diagnostics: no locals, source lines, environment dumps or credentials."""
import base64 as _rd_b64
import hashlib as _rd_hash
import json as _rd_json
import os as _rd_os
import re as _rd_re
import sys as _rd_sys
from pathlib import Path as _rd_Path

_RBDIAG = {'root': None, 'stage': 'module-start', 'sequence': 0, 'secrets': set()}


def _rbdiag_remember(value):
    if isinstance(value, bytes):
        variants = [value.hex(), repr(value), _rd_b64.b64encode(value).decode()]
        try:
            variants.append(value.decode())
        except UnicodeDecodeError:
            pass
    else:
        variants = [str(value)]
    _RBDIAG['secrets'].update(v for v in variants if v)
    return value


def _rbdiag_text(value):
    text = str(value)
    for secret in sorted(_RBDIAG['secrets'], key=len, reverse=True):
        text = text.replace(secret, '<redacted>')
    # Never expose URL userinfo, query strings, SAS signatures or bearer tokens.
    text = _rd_re.sub(r'https?://[^\s\'\"<>]+', '<redacted-url>', text, flags=_rd_re.I)
    text = _rd_re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer <redacted>', text)
    text = _rd_re.sub(r'''(?ix)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential|authorization|RB_DISPATCH_HMAC|LLM_API_KEY|sig)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1<redacted>', text)
    text = _rd_re.sub(r'[A-Za-z0-9_+/=-]{40,}', '<redacted-opaque-value>', text)
    return text[:4096]


def _rbdiag_frame_identifier(value):
    text = str(value)
    for secret in sorted(_RBDIAG['secrets'], key=len, reverse=True):
        text = text.replace(secret, '<redacted>')
    return _rd_re.sub(r'[^A-Za-z0-9_.<>-]', '_', text)[:200]


def _rbdiag_safe(value, depth=0):
    if depth > 12:
        return '<depth-bound>'
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:1024]:
            name = _rbdiag_text(key)
            if name in ('file', 'function') and isinstance(item, str):
                result[name] = _rbdiag_frame_identifier(item)
            elif name in ('credential_values_logged', 'locals_logged') and type(item) is bool:
                result[name] = item
            elif _rd_re.search(r'(?i)(secret|password|credential|authorization|token|api[_-]?key)', name):
                result[name] = '<redacted>'
            else:
                result[name] = _rbdiag_safe(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_rbdiag_safe(v, depth + 1) for v in value[:1024]]
    return _rbdiag_text(value)


def _rbdiag_exception_data(exc):
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append({'file': _rbdiag_frame_identifier(_rd_Path(code.co_filename).name),
                       'function': _rbdiag_frame_identifier(code.co_name), 'line': tb.tb_lineno})
        tb = tb.tb_next
    record = {'schema': 'reviewbench-sanitized-exception/v1', 'stage': _RBDIAG['stage'],
              'exception_type': type(exc).__name__, 'message': _rbdiag_text(exc),
              'frames': frames[-32:], 'locals_logged': False, 'credential_values_logged': False}
    for name in ('code', 'errno'):
        value = getattr(exc, name, None)
        if type(value) is int:
            record[name] = value
    return record


def _rbdiag_emit(name, value):
    """Diagnostic I/O cannot replace an original evaluation/guard exception."""
    try:
        if _RBDIAG['root'] is None:
            return
        directory = _RBDIAG['root'] / 'diagnostics-runtime'
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != _rd_os.geteuid():
            raise RuntimeError('unsafe diagnostic directory')
        _RBDIAG['sequence'] += 1
        label = _rd_re.sub('[^a-zA-Z0-9_.-]', '_', name)[:100]
        path = directory / ('%04d-%s.json' % (_RBDIAG['sequence'], label))
        raw = (_rd_json.dumps(_rbdiag_safe(value), sort_keys=True) + '\n').encode()
        if len(raw) > 2 * 1024**2:
            raw = (_rd_json.dumps({'stage': _RBDIAG['stage'], 'snapshot_oversize': True,
                                  'bytes': len(raw), 'sha256': _rd_hash.sha256(raw).hexdigest()}) + '\n').encode()
        fd = _rd_os.open(str(path), _rd_os.O_CREAT | _rd_os.O_EXCL | _rd_os.O_WRONLY | _rd_os.O_NOFOLLOW, 0o600)
        with _rd_os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            _rd_os.fsync(stream.fileno())
    except Exception as failure:
        print(_rd_json.dumps({'status': 'DIAGNOSTIC_WRITE_FAILED',
                              'diagnostic': _rbdiag_exception_data(failure)}), file=_rd_sys.stderr)


def _rbdiag_begin(root, stage):
    _RBDIAG['root'] = _rd_Path(root)
    _rbdiag_phase(stage)


def _rbdiag_phase(stage):
    _RBDIAG['stage'] = stage
    _rbdiag_emit('phase', {'stage': stage})


def _rbdiag_exception(exc):
    value = _rbdiag_exception_data(exc)
    _rbdiag_emit('exception', value)
    return value


def _rbdiag_resources(runtime, kit, plan, root, stage="prepare.resource_check"):
    _rbdiag_phase(stage)
    scope = runtime.observe_resources.__globals__
    original = scope['resource_check']
    def before_check(bound_plan, host):
        _rbdiag_emit('RESOURCE_OBSERVATION_BEFORE_CHECK', {'host': host,
                     'limits': bound_plan['node_resource_limits'], 'request': bound_plan['resource_request']})
        return original(bound_plan, host)
    scope['resource_check'] = before_check
    try:
        return runtime.observe_resources(kit, plan, root)
    finally:
        scope['resource_check'] = original


def _rbdiag_node_clean(physical, root, plan, kit, env, stage="prepare.node_clean"):
    _rbdiag_phase(stage)
    scope = physical.node_clean.__globals__
    original = scope['reused']
    patches = []
    def capture_reused(*args, **kwargs):
        paths, observer = original(*args, **kwargs)
        capture = observer.capture
        def before_check(*args, **kwargs):
            value = capture(*args, **kwargs)
            _rbdiag_emit('NODE_OBSERVATION_BEFORE_CHECK', value)
            return value
        observer.capture = before_check
        patches.append((observer, capture))
        return paths, observer
    scope['reused'] = capture_reused
    try:
        return physical.node_clean(root, plan, kit, env)
    finally:
        scope['reused'] = original
        for observer, capture in reversed(patches):
            observer.capture = capture


def _rbdiag_upload_failure(member, exc):
    value = {'status': 'DIAGNOSTIC_UPLOAD_FAILED', 'member': _rbdiag_text(member),
             'diagnostic': _rbdiag_exception_data(exc), 'original_failure_preserved': True}
    _rbdiag_emit('upload_failure', value)
    print(_rd_json.dumps(value), file=_rd_sys.stderr)
