#!/usr/bin/env python3
"""Pure, pinned byte transform for a NEW native node carrier (never a retry).

C3's Docker 29 private fallback uses <root>/x/extensions/callback.sock: 110
bytes for Speckle, beyond Linux sockaddr_un.sun_path. Retain the canonical
root, immutable probe/archive/image, containerd, endpoint and cleanup custody;
compact only dockerd's exec-root when that implicit callback cannot fit.

Supply integration: project_node(..., transform=transform). No extra resource
or import is needed on the node: helpers are embedded in IMAGE286.py and the
ADAPTER286.py cleanup consumer receives that exact new image-helper SHA/size.
No task submission, grants, model calls, once mutation or disk writes here.
"""
from __future__ import annotations

import ast
import hashlib

ORIGINAL_IMAGE_SHA256 = 'e6177bd10fad07f44c413fe1925abe34ba09cfd5cadf3d05685c1e0f0284c134'
ORIGINAL_IMAGE_BYTES = 18178
# Populated from the checked, deterministic patch; regression verifies both.
PATCHED_IMAGE_SHA256 = '8078896a3b157f056e084877a49fc4804ee53ae11f62ee8bdedbe8c140fa2360'
PATCHED_IMAGE_BYTES = 22306

RUNTIME_SOURCE = r'''
def native_node_exec_root(root):
    """Plan a deterministic OS-disk exec directory, not a workload-root remap."""
    root = Path(root)
    if (not root.is_absolute() or root.resolve() != root or root.is_symlink()
            or root.parts[:4] != ('/', 'var', 'lib', 'reviewbench399')
            or len(root.parts) != 6 or '\x00' in str(root)):
        raise ValueError('NATIVE_EXEC_ROOT_REQUIRES_CANONICAL_TASK_ROOT')
    legacy = root / 'x'
    if len(os.fsencode(legacy / 'extensions/callback.sock')) < 108:
        return legacy
    return root.parent.parent / '.exec' / hashlib.sha256(os.fsencode(root)).hexdigest()[:32]


def native_node_owned_directory(path):
    """Reject aliases, foreign ownership and writable parents before reservation."""
    path = Path(path)
    if path.resolve() != path:
        raise ValueError('NATIVE_EXEC_DIRECTORY_ALIAS')
    for parent in (path, *path.parents):
        st = parent.lstat()
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_mode & 0o022):
            raise ValueError('NATIVE_EXEC_DIRECTORY_NOT_PRIVATE:' + str(parent))
    return path


def native_node_reserve_exec(root, endpoint):
    """Fresh directory only. Never adopt/delete a prior attempt or its evidence."""
    root, endpoint = Path(root), Path(endpoint)
    if endpoint != native_node_exec_root(root) or endpoint == root / 'x':
        raise ValueError('NATIVE_EXEC_RESERVATION_BINDING')
    base = root.parent.parent
    native_node_owned_directory(base)
    # A shared *parent*, not a shared daemon/runtime/lease or execution identity.
    try:
        (base / '.exec').mkdir(mode=0o700)
    except FileExistsError:
        pass
    native_node_owned_directory(base / '.exec')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(base / '.exec', flags)
    try:
        try:
            os.mkdir(endpoint.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError as error:
            raise RuntimeError('NATIVE_EXEC_ALREADY_PRESENT_NO_REPLAY') from error
        fd = os.open(endpoint.name, flags, dir_fd=parent_fd)
        try:
            st = os.fstat(fd)
            if st.st_dev != root.stat().st_dev or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise ValueError('NATIVE_EXEC_OWNERSHIP_OR_DISK_DOMAIN')
            body = {'schema': 'reviewbench-native-exec-root/v1', 'root': str(root),
                    'exec_root': str(endpoint), 'original_exec_root': str(root / 'x'),
                    'native_authorized': False, 'automatic_retry': False}
            raw = (json.dumps(body, sort_keys=True) + '\n').encode()
            owner = os.open('OWNER.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=fd)
            with os.fdopen(owner, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.fsync(fd)
            # This stays in the original task's diagnostic/cleanup custody domain.
            with (root / 'ii/NATIVE_EXEC_ROOT.json').open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        finally:
            os.close(fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def native_node_daemon_config(config, root):
    """Only exec-root changes; network, image, resources and lifetime do not."""
    root = Path(root)
    if (config.get('exec-root') != str(root / 'x')
            or config.get('data-root') != str(root / 'ii/d')
            or config.get('pidfile') != str(root / 'ii/p')
            or config.get('hosts') != ['unix://' + str(root / 'ii/s')]):
        raise ValueError('NATIVE_EXEC_ORIGINAL_CONFIG_DRIFT')
    endpoint = native_node_exec_root(root)
    callback = endpoint / 'extensions/callback.sock'
    if len(os.fsencode(callback)) >= 108:
        raise ValueError('NATIVE_EXEC_CALLBACK_PATH_TOO_LONG')
    if endpoint != root / 'x':
        native_node_reserve_exec(root, endpoint)
    return {**config, 'exec-root': str(endpoint)}
'''


def patch_image_wrapper(raw: bytes) -> bytes:
    """Authenticate the known deployed helper, add one path-only call site."""
    if len(raw) != ORIGINAL_IMAGE_BYTES or hashlib.sha256(raw).hexdigest() != ORIGINAL_IMAGE_SHA256:
        raise ValueError('UNREVIEWED_IMAGE_WRAPPER_BYTES')
    text = raw.decode('utf-8')
    before = "        return {**old_config(paths), 'containerd': str(extra['socket']), 'log-level': 'info'}"
    after = "        return {**native_node_daemon_config(old_config(paths), root), 'containerd': str(extra['socket']), 'log-level': 'info'}"
    if text.count(before) != 1 or text.count('\ndef main():') != 1:
        raise ValueError('EXACT_PRIVATE_DAEMON_SEAM_REQUIRED')
    # All other original functions and checks stay byte-for-byte unchanged.
    result = text.replace(before, after).replace('\ndef main():', RUNTIME_SOURCE + '\n\ndef main():').encode()
    ast.parse(result)
    return result


def _assignment(text: str, name: str, expected, value) -> str:
    nodes = [n for n in ast.parse(text).body if isinstance(n, ast.Assign)
             and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name]
    if len(nodes) != 1 or ast.literal_eval(nodes[0].value) != expected:
        raise ValueError('IMAGE_CLEANUP_PIN_DRIFT:' + name)
    node = nodes[0]; lines = text.splitlines(True)
    lines[node.lineno-1:node.end_lineno] = [name + ' = ' + repr(value) + '\n']
    return ''.join(lines)


def transform(file_name: str, raw_bytes: bytes, descriptor: dict) -> bytes:
    """Stateless project_node hook; physical descriptor is never changed here.

    The producer authenticates the full source closure first. Unknown families
    fail closed on IMAGE/ADAPTER pins instead of accepting a similar wrapper.
    Original role names/count, PLAN, model/proxy/Oracle/G/C/J/once are unchanged.
    """
    if file_name == 'IMAGE286.py':
        result = patch_image_wrapper(raw_bytes)
        if len(result) != PATCHED_IMAGE_BYTES or hashlib.sha256(result).hexdigest() != PATCHED_IMAGE_SHA256:
            raise ValueError('NODE_ADAPTER_RELEASE_PIN_DRIFT')
        return result
    if file_name == 'ADAPTER286.py':
        text = _assignment(raw_bytes.decode(), 'IMAGE_SHA275', ORIGINAL_IMAGE_SHA256, PATCHED_IMAGE_SHA256)
        text = _assignment(text, 'IMAGE_BYTES275', ORIGINAL_IMAGE_BYTES, PATCHED_IMAGE_BYTES)
        return text.encode()
    return raw_bytes

# Second, independent adapter layer: retain the frozen callback repair above.
CALLBACK_TRANSFORM = transform
API_INPUT_SHA256 = PATCHED_IMAGE_SHA256
API_INPUT_BYTES = PATCHED_IMAGE_BYTES
API_OUTPUT_SHA256 = '4eb3d56e3bc3060431943fbd4dacf8e659ef26013038992309d5f0466d9fb04e'
API_OUTPUT_BYTES = 26763
API_READY_SOURCE = r'''
def native_node_wait_api(paths, daemon, deadline):
    """Readiness-only HEAD, bounded by the ORIGINAL 90s startup deadline.

    Socket creation is not API readiness. No Docker work, API version/model
    changes, daemon restart, timeout extension or accepted-step replay here.
    """
    started = time.monotonic()
    need(0 < deadline - started <= 90, 'invalid original daemon readiness deadline')
    result = {'event': 'private_docker_api_readiness', 'endpoint': str(paths['socket']),
              'request': 'HEAD /_ping', 'attempts': 0, 'ready': False,
              'original_deadline_monotonic': deadline, 'started_monotonic': started,
              'native_authorized': False, 'model_posts': 0}
    try:
        while True:
            need(daemon.poll() is None, 'private daemon exited before API readiness')
            remaining = deadline - time.monotonic()
            need(remaining > 0, 'private Docker API not ready within original startup deadline')
            result['attempts'] += 1
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(min(1.0, remaining))
                    probe.connect(str(paths['socket']))
                    peer_pid, peer_uid, _ = struct.unpack('3i', probe.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    need(peer_pid == daemon.pid and peer_uid == os.geteuid(),
                         'private API endpoint not owned by original daemon')
                    probe.sendall(b'HEAD /_ping HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n')
                    response = bytearray()
                    while b'\r\n\r\n' not in response:
                        remaining = deadline - time.monotonic()
                        need(remaining > 0, 'private Docker API not ready within original startup deadline')
                        probe.settimeout(min(1.0, remaining))
                        block = probe.recv(min(1024, 8193-len(response)))
                        if not block:
                            break
                        response.extend(block)
                        need(len(response) <= 8192, 'bounded private API readiness headers')
                    status = bytes(response).split(b'\r\n', 1)[0].split()
                    if (b'\r\n\r\n' in response and len(status) >= 2
                            and status[0] in (b'HTTP/1.0', b'HTTP/1.1') and status[1] == b'200'):
                        need(time.monotonic() <= deadline and daemon.poll() is None,
                             'private API became ready only after deadline or daemon exit')
                        result['ready'] = True
                        return
                    result['last_error'] = 'private_ping_not_200'
            except (ConnectionRefusedError, FileNotFoundError, TimeoutError,
                    ConnectionResetError, BrokenPipeError) as error:
                result['last_error'] = type(error).__name__
            remaining = deadline-time.monotonic()
            if remaining > 0:
                time.sleep(min(.1, remaining))
    finally:
        result['ended_monotonic'] = time.monotonic()
        # Existing TASK diagnostics/returned_evidence already collect root/events.
        events = paths['base'].parent / 'events'
        events.mkdir(exist_ok=True, mode=0o700)
        save(events / 'PRIVATE_DOCKER_API_READY.json', result)


def bind_private_api_ready(module, original_source):
    """Keep the authenticated original main, inject one pre-info readiness gate."""
    import ast
    fn = next(n for n in ast.parse(original_source).body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    text = ast.get_source_segment(original_source, fn)
    before = '        private = [docker, "--host", "unix://" + str(paths["socket"])]'
    need(text.count(before) == 1 and text.count('deadline = time.monotonic() + 90') == 1,
         'original startup/first-info seam changed')
    text = text.replace(before, '        NATIVE_NODE_WAIT_API(paths, daemon, deadline)\n' + before)
    module.__dict__['NATIVE_NODE_WAIT_API'] = native_node_wait_api
    exec(compile(text, str(Path(__file__).resolve()) + ':original-main-api-readiness', 'exec'), module.__dict__)
    need(module.main.__globals__['NATIVE_NODE_WAIT_API'] is native_node_wait_api,
         'actual original main readiness consumer disconnected')
'''


def patch_ready_wrapper(raw_bytes):
    if len(raw_bytes) != API_INPUT_BYTES or hashlib.sha256(raw_bytes).hexdigest() != API_INPUT_SHA256:
        raise ValueError('UNREVIEWED_CALLBACK_REPAIRED_WRAPPER')
    text = raw_bytes.decode()
    before = '    bind_private_socket_guard(module, raw.decode())\n'
    if text.count(before) != 1 or text.count('\ndef main():') != 1:
        raise ValueError('EXACT_ORIGINAL_PROBE_LOAD_SEAM')
    text = text.replace(before, before + '    bind_private_api_ready(module, raw.decode())\n')
    text = text.replace('\ndef main():', API_READY_SOURCE + '\n\ndef main():')
    ast.parse(text)
    return text.encode()


def transform(file_name: str, raw_bytes: bytes, descriptor: dict) -> bytes:
    """Pure supplier hook: supports original or callback-repaired source family.

    No extra manifest resource/role. The callback layer remains identical and
    IMAGE_SHA275/IMAGE_BYTES275 in its cleanup consumer advance atomically.
    Unknown or already-transformed identities are refused rather than repatched.
    """
    if file_name == 'IMAGE286.py':
        if hashlib.sha256(raw_bytes).hexdigest() == ORIGINAL_IMAGE_SHA256:
            raw_bytes = CALLBACK_TRANSFORM(file_name, raw_bytes, descriptor)
        result = patch_ready_wrapper(raw_bytes)
        if hashlib.sha256(result).hexdigest() != API_OUTPUT_SHA256 or len(result) != API_OUTPUT_BYTES:
            raise ValueError('API_READINESS_RELEASE_PIN_DRIFT')
        return result
    if file_name == 'ADAPTER286.py':
        tree = ast.parse(raw_bytes)
        values = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and len(n.targets) == 1
                  and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ('IMAGE_SHA275', 'IMAGE_BYTES275')}
        if values.get('IMAGE_SHA275') == ORIGINAL_IMAGE_SHA256:
            raw_bytes = CALLBACK_TRANSFORM(file_name, raw_bytes, descriptor)
        text = _assignment(raw_bytes.decode(), 'IMAGE_SHA275', API_INPUT_SHA256, API_OUTPUT_SHA256)
        return _assignment(text, 'IMAGE_BYTES275', API_INPUT_BYTES, API_OUTPUT_BYTES).encode()
    return raw_bytes
