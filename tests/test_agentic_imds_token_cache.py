"""Offline token/PUT contract tests; no sockets, models, claims, or cloud writes.

Most cache and transform tests are self-contained. Original-function integration
reads the exact pinned wave4 kit when available; it never imports its controllers.
The --probe ADAPTER path mode emits a small reproducible transaction observation.
"""
from __future__ import annotations

import ast
import base64
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
from types import ModuleType, SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / 'scripts/agentic_imds_token_cache.py'
NODE = PROJECT / '.benchmarks/framework-repair-20261012-v1/integration/production-live/native-waves/0004/cohort/lanes/0e54eb1609360a93be55/node'


def module(raw, path):
    value = ModuleType(path.stem)
    value.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), value.__dict__)
    return value


T = module(SCRIPT.read_bytes(), SCRIPT)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Clock:
    wall = 1700000000.0
    mono = 100.0

    def __init__(self):
        self.sleeps = []

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class Response(io.BytesIO):
    def __init__(self, raw=b'', status=201, headers=None):
        super().__init__(raw)
        self.status = status
        self.headers = {'ETag': '"fixture-etag"'} if headers is None else headers


class Wire:
    def __init__(self, clock):
        self.clock = clock
        self.gets = []
        self.puts = []
        self.token_effects = []
        self.put_effect = None
        self.handlers = []
        self.record = None

    def build_opener(self, *handlers):
        self.handlers.append(handlers)
        return self

    def open(self, request, timeout):
        if request.get_method() == 'GET':
            self.gets.append((request, timeout))
            if self.token_effects:
                effect = self.token_effects.pop(0)
                if isinstance(effect, BaseException):
                    raise effect
                if callable(effect):
                    return effect()
                return Response(json.dumps(effect).encode(), 200)
            record = self.record if self.record is not None else {
                'access_token': 'fixture-secret-access-token',
                'expires_on': str(self.clock.time() + 3600), 'expires_in': '3600'}
            return Response(json.dumps(record).encode(), 200)
        assert request.get_method() == 'PUT'
        self.puts.append((request, timeout))
        if isinstance(self.put_effect, BaseException):
            raise self.put_effect
        return self.put_effect if self.put_effect is not None else Response()


def runtime():
    scope = {}
    exec(compile(T.RUNTIME_SOURCE, 'embedded-imds-test', 'exec'), scope)
    clock = Clock()
    scope['_imds_time'] = clock
    return scope, clock, Wire(clock)


def get(scope, wire, identity='identity-a', resource=None, endpoint=None):
    return scope['_imds_token'](wire, identity, resource or scope['_IMDS_RESOURCE'],
                                 endpoint or scope['_IMDS_ENDPOINT'], json.loads)


def http_error(code, retry=None):
    return urllib.error.HTTPError('http://fixture.invalid/token', code, 'fixture HTTP error',
                                  {} if retry is None else {'Retry-After': retry}, io.BytesIO(b''))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)

    def forbidden(*args, **kwargs):
        raise AssertionError('token cache tests may not open network sockets')

    monkeypatch.setattr(socket, 'socket', forbidden)


@pytest.fixture
def original():
    if not (NODE / 'DESCRIPTOR.json').exists():
        pytest.skip('optional read-only wave4 canonical kit is unavailable')
    descriptor = json.loads((NODE / 'DESCRIPTOR.json').read_bytes())
    kit = Path(descriptor['canonical_plan']['path']).parent
    plan_raw = (kit / 'PLAN.json').read_bytes()
    assert sha(plan_raw) == descriptor['canonical_plan']['sha256']
    path = kit / 'reuse/physical_node.py'
    raw = path.read_bytes()
    assert (sha(raw), len(raw)) == (T.PHYSICAL_SHA256, T.PHYSICAL_BYTES)
    return SimpleNamespace(descriptor=descriptor, kit=kit, path=path, raw=raw,
                           plan=json.loads(plan_raw))


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect refused', headers, fp)


def physical(original, wire):
    # Same AST/private-globals mechanism as the canonical R.physical loader;
    # also load create_once to explicitly test its original globals reference.
    common = SimpleNamespace(
        PREFIX='fixture', PARENT='parent', STORAGE='https://fixture.invalid/container/',
        MI_CLIENT='identity-a', need=need, decode=json.loads, sha=sha,
        integer=lambda value, expected: need(type(value) is int and value == expected, 'status differs'),
        encoded=lambda value: (json.dumps(value, sort_keys=True) + '\n').encode())
    request = SimpleNamespace(Request=urllib.request.Request, ProxyHandler=urllib.request.ProxyHandler,
                              build_opener=wire.build_opener)
    scope = dict(C=common, urllib=SimpleNamespace(request=request, parse=urllib.parse),
                 NoRedirect=NoRedirect, base64=base64, hashlib=hashlib, os=os)
    names = {'put_blob', 'create_once', 'actual_node', 'reused', 'clean_env', 'owned_cleanup'}
    nodes = [n for n in ast.parse(original.raw).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(original.path), 'exec'), scope)
    return SimpleNamespace(**{name: scope[name] for name in names}), common


def install(original):
    scope, clock, wire = runtime()
    value, common = physical(original, wire)
    before = value.put_blob
    scope['_imds_install'](value, original.kit, original.plan)
    return SimpleNamespace(scope=scope, clock=clock, wire=wire, value=value,
                           common=common, before=before)


def adapter_closure(raw, original, wire, clock):
    """Execute the actual adapter's nested physical closure (not a new wrapper).

    Outer grant/identity installation is not authorized by these offline tests;
    closure dependencies are inert. The original physical functions, token copy,
    source pins, namespace checks, and PUT protocol are real executable code.
    """
    adapter = module(raw, NODE / 'ADAPTER286.py')
    scope = adapter.__dict__
    scope['_imds_time'] = clock
    value, common = physical(original, wire)
    scope.update(d={'native_root': '/fixture'}, bind_plan=lambda d, p: None,
                 original_physical=lambda kit, plan: value)
    install_node = next(n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name == 'install')
    closure = next(n for n in install_node.body if isinstance(n, ast.FunctionDef) and n.name == 'physical')
    exec(compile(ast.Module(body=[closure], type_ignores=[]), str(NODE/'ADAPTER286.py'), 'exec'), scope)
    return scope['physical'](original.kit, original.plan), common


def test_many_uploads_cache_but_never_changes_original_request_contract(original):
    case = install(original)
    for index in range(100):
        body = ('payload-' + str(index)).encode()
        result = case.value.put_blob('fixture/a b/' + str(index), body)
        request, timeout = case.wire.puts[-1]
        assert request.full_url == 'https://fixture.invalid/container/fixture/a%20b/' + str(index)
        assert request.data == body and timeout == 300
        assert dict(request.header_items()) == {
            'Authorization': 'Bearer fixture-secret-access-token', 'X-ms-version': '2023-11-03',
            'X-ms-blob-type': 'BlockBlob', 'If-none-match': '*', 'X-ms-meta-sha256': sha(body),
            'Content-md5': base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()}
        assert result == {'blob': 'fixture/a b/' + str(index), 'bytes': len(body),
                          'sha256': sha(body), 'etag': '"fixture-etag"'}
    assert len(case.wire.gets) == 1
    request, timeout = case.wire.gets[0]
    assert timeout == 15 and dict(request.header_items()) == {'Metadata': 'true'}
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) == {
        'api-version': ['2018-02-01'], 'resource': ['https://storage.azure.com/'], 'client_id': ['identity-a']}
    assert all(handlers[0].proxies == {} and isinstance(handlers[1], NoRedirect)
               for handlers in case.wire.handlers)
    assert original.path.read_bytes() == original.raw


def test_cache_shared_by_physical_instances_and_original_create_once(original):
    case = install(original)
    other, _ = physical(original, case.wire)
    case.scope['_imds_install'](other, original.kit, original.plan)
    case.value.put_blob('fixture/result', b'one')
    other.create_once('fixture/native-once/parent.json', {'native': 'unchanged'})
    assert len(case.wire.gets) == 1 and len(case.wire.puts) == 2
    assert case.before.__globals__['put_blob'] is case.value.put_blob
    assert case.value.create_once.__globals__['put_blob'] is case.value.put_blob
    with pytest.raises(ValueError, match='first-start key may not reset'):
        case.value.create_once('fixture/native-once/other.json', {})
    assert len(case.wire.puts) == 2


def test_only_put_blob_seam_changes_and_no_credentials_persist(original, monkeypatch, capsys):
    scope, clock, wire = runtime()
    value, common = physical(original, wire)
    common.consumed = {'G': True, 'C': False, 'native_once': 'retained'}
    original_functions = dict(vars(value))
    before_common = dict(vars(common))
    before_environment = dict(os.environ)
    scope['_imds_install'](value, original.kit, original.plan)
    for name, function in original_functions.items():
        if name != 'put_blob':
            assert getattr(value, name) is function
    def no_write(*args, **kwargs):
        raise AssertionError('token access must not use disk storage')
    monkeypatch.setattr('builtins.open', no_write)
    monkeypatch.setattr(Path, 'open', no_write)
    monkeypatch.setattr(os, 'open', no_write)
    value.put_blob('fixture/result', b'payload')
    value.put_blob('fixture/result2', b'payload')
    assert vars(common) == before_common and dict(os.environ) == before_environment
    assert capsys.readouterr() == ('', '')
    # Every original code site after token acquisition is literally retained.
    function = next(n for n in ast.parse(original.raw).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'put_blob')
    text = ast.get_source_segment(original.raw.decode(), function)
    modified = text.replace(scope['_IMDS_TOKEN_BLOCK'], scope['_IMDS_TOKEN_CALL'])
    tail = '    C.need(token.encode() not in body, "credential echo refused")'
    assert modified[modified.index(tail):] == text[text.index(tail):]


def test_renewed_token_is_also_checked_for_payload_echo(original):
    case = install(original)
    case.value.put_blob('fixture/first', b'payload')
    case.clock.advance(3600)
    case.wire.record = {'access_token': 'renewed-fixture-token', 'expires_in': 3600}
    with pytest.raises(ValueError, match='credential echo refused'):
        case.value.put_blob('fixture/next', b'prefix:renewed-fixture-token:suffix')
    assert len(case.wire.gets) == 2 and len(case.wire.puts) == 1


def test_expiry_safety_margin_and_actual_renewal():
    scope, clock, wire = runtime()
    get(scope, wire)
    clock.advance(3539)
    get(scope, wire)
    assert len(wire.gets) == 1
    clock.advance(1)
    get(scope, wire)
    assert len(wire.gets) == 2


@pytest.mark.parametrize('field', ['expires_on', 'expires_in'])
def test_each_real_expiry_field_suffices(field):
    scope, clock, wire = runtime()
    wire.record = {'access_token': 'fixture-token', field: str(clock.time()+120 if field == 'expires_on' else 120)}
    get(scope, wire)
    clock.advance(59)
    get(scope, wire)
    assert len(wire.gets) == 1
    clock.advance(1)
    get(scope, wire)
    assert len(wire.gets) == 2


@pytest.mark.parametrize('expiry', [None, {}, {'expires_on': None}, {'expires_in': True},
    {'expires_in': 'nan'}, {'expires_on': 'inf'}, {'expires_in': '-1'}, {'expires_in': '0'},
    {'expires_on': '10'}, {'expires_in': 'nonsense'}, {'expires_in': []},
    {'expires_in': '120', 'expires_on': 'bad'}])
def test_no_assumed_or_invalid_expiry(expiry):
    scope, clock, wire = runtime()
    wire.record = {'access_token': 'fixture-token', **(expiry or {})}
    with pytest.raises(ValueError, match='IMDS_TOKEN_CACHE:') as error:
        get(scope, wire)
    assert 'fixture-token' not in str(error.value)
    assert scope['_IMDS_STATE'][2] == {}
    assert len(wire.gets) == 1


def test_short_lived_token_used_once_but_not_cached():
    scope, clock, wire = runtime()
    wire.record = {'access_token': 'short-lived-fixture', 'expires_in': 30}
    get(scope, wire)
    get(scope, wire)
    assert len(wire.gets) == 2 and scope['_IMDS_STATE'][2] == {}


def test_minimum_expiry_and_network_latency_and_wall_clock_steps():
    scope, clock, wire = runtime()
    def delayed():
        record = {'access_token': 'fixture-token', 'expires_in': 120, 'expires_on': clock.time()+300}
        clock.advance(20)
        return Response(json.dumps(record).encode())
    wire.token_effects = [delayed]
    get(scope, wire)
    clock.advance(39)
    clock.wall -= 10000  # Must not extend the monotonic deadline.
    get(scope, wire)
    assert len(wire.gets) == 1
    clock.advance(1)
    get(scope, wire)
    assert len(wire.gets) == 2
    clock.wall += 100000
    get(scope, wire)
    assert len(wire.gets) == 3


def test_identity_resource_endpoint_and_pid_are_distinct_cache_keys():
    scope, clock, wire = runtime()
    get(scope, wire)
    get(scope, wire, identity='identity-b')
    get(scope, wire, resource='https://other-resource.invalid/')
    get(scope, wire, endpoint='http://other-endpoint.invalid/token')
    assert len(wire.gets) == 4
    assert set(scope['_IMDS_STATE'][2]) == {
        (os.getpid(), 'identity-a', scope['_IMDS_RESOURCE'], scope['_IMDS_ENDPOINT']),
        (os.getpid(), 'identity-b', scope['_IMDS_RESOURCE'], scope['_IMDS_ENDPOINT']),
        (os.getpid(), 'identity-a', 'https://other-resource.invalid/', scope['_IMDS_ENDPOINT']),
        (os.getpid(), 'identity-a', scope['_IMDS_RESOURCE'], 'http://other-endpoint.invalid/token')}
    scope['_imds_os'] = SimpleNamespace(getpid=lambda: os.getpid()+1)
    get(scope, wire)
    assert len(wire.gets) == 5 and len(scope['_IMDS_STATE'][2]) == 1


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires fork')
def test_real_fork_discards_parent_token_and_inherited_locked_lock():
    scope, clock, wire = runtime()
    get(scope, wire)
    reader, writer = os.pipe()
    lock = scope['_IMDS_STATE'][1]
    with lock:
        pid = os.fork()
        if pid == 0:
            try:
                os.close(reader)
                get(scope, wire)
                ok = len(wire.gets) == 2 and scope['_IMDS_STATE'][0] == os.getpid()
                os.write(writer, b'fresh-child-token' if ok else b'bad')
                os._exit(0 if ok else 1)
            except BaseException:
                os._exit(2)
    os.close(writer)
    try:
        if not select.select([reader], [], [], 5)[0]:
            os.kill(pid, signal.SIGKILL)
            pytest.fail('child waited on an inherited metadata lock')
        assert os.read(reader, 100) == b'fresh-child-token'
    finally:
        os.close(reader)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    get(scope, wire)
    assert len(wire.gets) == 1


def test_concurrent_uploads_single_flight(original):
    case = install(original)
    with ThreadPoolExecutor(max_workers=16) as workers:
        results = list(workers.map(lambda i: case.value.put_blob('fixture/' + str(i), b'payload'), range(128)))
    assert len(results) == len(case.wire.puts) == 128
    assert len(case.wire.gets) == 1
    case.clock.advance(3600)
    with ThreadPoolExecutor(max_workers=16) as workers:
        list(workers.map(lambda i: case.value.put_blob('fixture/next/' + str(i), b'payload'), range(64)))
    assert len(case.wire.gets) == 2


def test_429_success_then_cached():
    scope, clock, wire = runtime()
    wire.token_effects = [http_error(429, '2'), http_error(429)]
    get(scope, wire)
    get(scope, wire)
    assert len(wire.gets) == 3 and clock.sleeps == [2.0, 1.0]


def test_429_exhaustion_is_bounded_and_propagates_same_error():
    scope, clock, wire = runtime()
    errors = [http_error(429, '5') for _ in range(4)]
    wire.token_effects = list(errors)
    with pytest.raises(urllib.error.HTTPError) as result:
        get(scope, wire)
    assert result.value is errors[-1]
    assert len(wire.gets) == 4 and clock.sleeps == [5.0, 5.0, 5.0]
    assert scope['_IMDS_STATE'][2] == {}


@pytest.mark.parametrize('hint', ['6', '1000', '-1', 'NaN', 'inf', 'invalid-date'])
def test_429_out_of_bound_or_bad_retry_after_never_retries_early(hint):
    scope, clock, wire = runtime()
    error = http_error(429, hint)
    wire.token_effects = [error]
    with pytest.raises(urllib.error.HTTPError) as result:
        get(scope, wire)
    assert result.value is error and len(wire.gets) == 1 and clock.sleeps == []


def test_retry_after_http_date_respected():
    scope, clock, wire = runtime()
    wire.token_effects = [http_error(429, formatdate(clock.time()+3, usegmt=True))]
    get(scope, wire)
    assert len(wire.gets) == 2 and clock.sleeps == [3.0]


@pytest.mark.parametrize('error', [http_error(401), http_error(403), http_error(400),
    http_error(404), http_error(301), http_error(500), http_error(503),
    urllib.error.URLError('fixture transport failure'), TimeoutError('fixture timeout')])
def test_non_429_errors_propagate_without_blind_retry(error):
    scope, clock, wire = runtime()
    wire.token_effects = [error]
    with pytest.raises(type(error)) as result:
        get(scope, wire)
    assert result.value is error and len(wire.gets) == 1 and clock.sleeps == []


@pytest.mark.parametrize('error', [TimeoutError('unknown PUT effect'), http_error(429),
    http_error(412), http_error(401), http_error(500), urllib.error.URLError('unknown PUT effect')])
def test_unknown_put_effect_or_http_failure_is_never_replayed(original, error):
    case = install(original)
    case.wire.put_effect = error
    with pytest.raises(type(error)) as result:
        case.value.put_blob('fixture/result', b'payload')
    assert result.value is error and len(case.wire.puts) == 1 and len(case.wire.gets) == 1
    assert case.clock.sleeps == []


@pytest.mark.parametrize('response', [Response(status=200), Response(headers={})])
def test_original_201_and_etag_checks_survive(original, response):
    case = install(original)
    case.wire.put_effect = response
    with pytest.raises(ValueError):
        case.value.put_blob('fixture/result', b'payload')
    assert len(case.wire.puts) == 1


def test_token_echo_and_namespace_checks_survive_on_cold_and_warm_cache(original, capsys):
    case = install(original)
    for _ in range(2):
        with pytest.raises(ValueError, match='credential echo refused'):
            case.value.put_blob('fixture/result', b'prefix:fixture-secret-access-token:suffix')
    for blob in ('foreign/result', 'fixture/../result'):
        with pytest.raises(ValueError, match='managed own result namespace required'):
            case.value.put_blob(blob, b'payload')
    assert len(case.wire.gets) == 1 and case.wire.puts == []
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('token', ['', None, 13, 'fixture\nsecret', 'fixture secret', 'fixture\x00secret'])
def test_invalid_tokens_fail_without_echo(token):
    scope, clock, wire = runtime()
    wire.record = {'access_token': token, 'expires_in': 3600}
    with pytest.raises(ValueError, match='invalid access token') as error:
        get(scope, wire)
    assert 'fixture' not in str(error.value)
    assert scope['_IMDS_STATE'][2] == {}


def test_malformed_or_oversized_json_and_read_failure_not_retried():
    for effect in (lambda: Response(b'{"access_token":"SECRET"'), lambda: Response(b'x'*131073)):
        scope, clock, wire = runtime()
        wire.token_effects = [effect]
        with pytest.raises(ValueError) as error:
            get(scope, wire)
        assert 'SECRET' not in str(error.value) and len(wire.gets) == 1
    scope, clock, wire = runtime()
    class BrokenRead(Response):
        def read(self, *args):
            raise TimeoutError('metadata body read timeout')
    wire.token_effects = [BrokenRead]
    with pytest.raises(TimeoutError):
        get(scope, wire)
    assert len(wire.gets) == 1


def test_cache_size_bound():
    scope, clock, wire = runtime()
    for i in range(150):
        get(scope, wire, identity=str(i))
    assert len(scope['_IMDS_STATE'][2]) == 128


def test_source_plan_and_loaded_callable_drift_fail_closed(original, tmp_path):
    scope, clock, wire = runtime()
    value, _ = physical(original, wire)
    plan = json.loads(json.dumps(original.plan))
    plan['files']['reuse/physical_node.py']['sha256'] = '0'*64
    with pytest.raises(ValueError, match='pin drift'):
        scope['_imds_install'](value, original.kit, plan)
    kit = tmp_path / 'kit'
    (kit / 'reuse').mkdir(parents=True)
    (kit / 'reuse/physical_node.py').write_bytes(original.raw.replace(b'If-None-Match', b'If-Xone-Match'))
    with pytest.raises(ValueError, match='source drift'):
        scope['_imds_install'](value, kit, original.plan)
    value.put_blob.__globals__['put_blob'] = lambda *a: None
    with pytest.raises(ValueError, match='globals drift'):
        scope['_imds_install'](value, original.kit, original.plan)
    value, _ = physical(original, wire)
    value.put_blob.__code__ = (lambda blob, body: None).__code__
    with pytest.raises(ValueError, match='code drift'):
        scope['_imds_install'](value, original.kit, original.plan)
    assert original.path.read_bytes() == original.raw


def synthetic_adapter(tmp_path):
    plan = {'files': {'reuse/physical_node.py': {'sha256': T.PHYSICAL_SHA256, 'bytes': T.PHYSICAL_BYTES}}}
    path = tmp_path / 'PLAN.json'
    path.write_text(json.dumps(plan))
    descriptor = {'canonical_plan': {'path': str(path), 'sha256': sha(path.read_bytes()), 'bytes': path.stat().st_size}}
    raw = ("ORIGINAL = " + repr({'physical_node.py': plan['files']['reuse/physical_node.py']}) + '''
def install(C,R,descriptor,descriptor_sha):
 d=descriptor
 original_physical=R.physical
 def physical(kit,plan):
  bind_plan(d,plan);physical=original_physical(kit,plan);original_actual=physical.actual_node
  return physical
 R.physical=physical
''').encode()
    return raw, descriptor


def test_transform_only_adapter_and_self_contained(tmp_path):
    raw, descriptor = synthetic_adapter(tmp_path)
    for name in ('TASK286.py', 'MODIFIED_FILE.py', 'IMAGE286.py', 'physical_node.py', 'other'):
        assert T.transform(name, raw, {}) is raw
    result = T.transform('ADAPTER286.py', raw, descriptor)
    adapted = module(result, tmp_path / 'ADAPTER286.py')
    assert callable(adapted._imds_install)
    assert 'agentic_imds_token_cache' not in result.decode()
    assert result.count(b'  _imds_install(physical,kit,plan)\n') == 1
    with pytest.raises(ValueError, match='already installed'):
        T.transform('ADAPTER286.py', result, descriptor)
    with pytest.raises(ValueError, match='closure seam drift'):
        T.transform('ADAPTER286.py', raw.replace(b'original_actual=', b'changed='), descriptor)
    with pytest.raises(ValueError, match='adapter physical source pin drift'):
        T.transform('ADAPTER286.py', raw.replace(T.PHYSICAL_SHA256.encode(), b'0'*64), descriptor)
    Path(descriptor['canonical_plan']['path']).write_bytes(b'{}')
    with pytest.raises(ValueError, match='canonical plan pin drift'):
        T.transform('ADAPTER286.py', raw, descriptor)


def test_after_actual_historical_diagnostic_transform_and_real_closure(original):
    raw = (NODE / 'ADAPTER286.py').read_bytes()  # Already diagnostic transformed.
    before_hash = sha(raw)
    result = T.transform('ADAPTER286.py', raw, original.descriptor)
    # Reconstruct the pre-diagnostic image pins/observer seam, then independently
    # execute the actual pinned predecessor to confirm composition ordering.
    diagnostic = module((PROJECT/'scripts/agentic_diagnostic_node_transform.py').read_bytes(),
                        PROJECT/'scripts/agentic_diagnostic_node_transform.py')
    node_transform = diagnostic.load('agentic_node_transform.py')
    image_before, _ = node_transform.images(original.descriptor)
    unowned = raw[:raw.index(b'\n\n# Owned runtime namespace observer copy;')]
    unowned = unowned.replace(b'return paths,_orn_observer(observe,kit,canonical_plan)', b'return paths,observe')
    unowned = node_transform.assignment(unowned.decode(), 'IMAGE_SHA275', sha(image_before))
    unowned = node_transform.assignment(unowned, 'IMAGE_BYTES275', len(image_before)).encode()
    composed = T.transform('ADAPTER286.py', diagnostic.transform('ADAPTER286.py', unowned, original.descriptor), original.descriptor)
    assert composed == result
    clock = Clock()
    wire = Wire(clock)
    value, _ = adapter_closure(result, original, wire, clock)
    value.put_blob('fixture/diagnostic', b'payload')
    value.create_once('fixture/native-once/parent.json', {'original': 'body'})
    assert len(wire.gets) == 1 and len(wire.puts) == 2
    assert sha((NODE/'ADAPTER286.py').read_bytes()) == before_hash
    assert original.path.read_bytes() == original.raw


def probe(adapter_path):
    descriptor = json.loads((NODE/'DESCRIPTOR.json').read_bytes())
    kit = Path(descriptor['canonical_plan']['path']).parent
    path = kit/'reuse/physical_node.py'
    original = SimpleNamespace(kit=kit, path=path, raw=path.read_bytes(),
                               plan=json.loads((kit/'PLAN.json').read_bytes()))
    need(sha(original.raw) == T.PHYSICAL_SHA256, 'probe canonical source pin drift')
    clock = Clock()
    wire = Wire(clock)
    value, _ = adapter_closure(Path(adapter_path).read_bytes(), original, wire, clock)
    for i in range(32):
        value.put_blob('fixture/diagnostics/' + str(i), b'identical-probe-payload')
    print(json.dumps({'uploads': len(wire.puts), 'imds_requests': len(wire.gets),
                      'conditional_puts': all(dict(r.header_items())['If-none-match'] == '*' for r, _ in wire.puts),
                      'original_sha256': sha(path.read_bytes()), 'source_unchanged': path.read_bytes() == original.raw}, sort_keys=True))


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--probe':
        raise SystemExit('usage: test_agentic_imds_token_cache.py --probe ADAPTER_PATH')
    probe(sys.argv[2])
