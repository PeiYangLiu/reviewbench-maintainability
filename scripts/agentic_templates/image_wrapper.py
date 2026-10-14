#!/usr/bin/env python3
"""Keep the exact image probe, but own its fallback containerd and lifecycle."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time
import stat

ORIGINAL_SHA = 'beaef1c7b4e7409581eaf613984a0c0143229c50842e144773fcef1b85775b3c'
ORIGINAL_BYTES = 25492
MAX_LOG = 8 * 1024**2
CONFIG_PROJECTION_SOURCE284 = 'def same_config278(actual, expected, parent):\n    """Compare Docker Config projections without weakening immutable identity.\n\n    Docker inspect may omit legacy/default Config fields retained in the\n    config blob. Only explicit no-op values and the separately pinned parent\n    image may be omitted. Nondefault, unknown, and meaningful fields remain\n    exact. Raw config digest, ordered layer digests, and platform stay checked.\n    """\n    require(isinstance(actual, dict) and isinstance(expected, dict), "Config must be an object")\n    defaults = {"AttachStderr": False, "AttachStdin": False, "AttachStdout": False,\n                "Domainname": "", "Entrypoint": None, "Hostname": "",\n                "OpenStdin": False, "StdinOnce": False, "Tty": False,\n                "User": "", "Volumes": None}\n    def projection(value):\n        result = dict(value)\n        for key, default in defaults.items():\n            if key in result and type(result[key]) is type(default) and result[key] == default:\n                result.pop(key)\n        if (isinstance(parent, str) and parent.startswith("sha256:") and len(parent) == 71\n                and result.get("Image") == parent):\n            result.pop("Image")\n        return result\n    return projection(actual) == projection(expected)\n\ndef verify_inspect(actual, expected):\n    require(isinstance(actual, list) and len(actual) == 1, "inspect must return exactly one image")\n    image = actual[0]\n    for field in ("Id", "RootFS", "Architecture", "Os", "Variant"):\n        require(image.get(field) == expected.get(field), "strict inspect mismatch: " + field)\n    require(same_config278(image.get("Config"), expected.get("Config"), expected.get("Parent")),\n            "strict inspect mismatch: Config")\n    require(image["Id"] == IMAGE, "inspect returned a different imageID")\n    return image\n\ndef check_config278(scan, packet):\n    require(packet["schema"] == "reviewbench-original-image-digest-expectation278/v1"\n            and packet["image_id"] == IMAGE and packet["config_sha256"] == IMAGE[7:]\n            and packet["archive_sha256"] == ARCHIVE_SHA and packet["archive_bytes"] == ARCHIVE_BYTES,\n            "foreign publication digest expectation")\n    raw = base64.b64decode(scan["config_raw_b64"], validate=True)\n    require(digest(raw) == scan["config_sha256"] == packet["config_sha256"],\n            "raw config digest differs from published imageID")\n    config = json.loads(raw)\n    require(config == scan["config"] and config["rootfs"]["type"] == "layers"\n            and same_config278(config["config"], packet["source_inspect"]["Config"], packet["source_inspect"].get("Parent"))\n            and config["rootfs"]["diff_ids"] == scan["rootfs_diff_ids"] == packet["source_inspect"]["RootFS"]["Layers"]\n            and config["architecture"] == packet["source_inspect"]["Architecture"]\n            and config["os"] == packet["source_inspect"]["Os"]\n            and config.get("variant") == packet["source_inspect"].get("Variant"),\n            "strict original config/ordered layers/platform differ")\n    return True\n'
CONFIG_PROJECTION_SOURCE_SHA284 = '5be2859705b87558149506b1a6049c7017b88e32197a4c28cd923cada5718414'


def need(value, reason):
    if not value:
        raise RuntimeError(reason)


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def proc(pid):
    p = Path('/proc') / str(pid)
    try:
        stat = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start': stat[19], 'state': stat[0],
                'argv': [os.fsdecode(v) for v in (p / 'cmdline').read_bytes().split(b'\0') if v],
                'pgid': os.getpgid(pid)}
    except FileNotFoundError:
        return None


def check_root(root):
    root = Path(root)
    need(root.is_absolute() and root.resolve() == root and not root.is_symlink()
         and root.parts[:4] == ('/', 'var', 'lib', 'reviewbench399') and len(root.parts) == 6,
         'exact canonical original task root required')
    return root


def owned_paths(root):
    root = check_root(root)
    base = root / 'ii'
    state = root / 'x' / 'containerd'
    # Retain canonical root, data/state directories, Docker endpoint and owner
    # records. Only the owned gRPC/debug addresses need a compact spelling.
    legacy = state / 'containerd.sock'
    managed = (legacy, state / 'containerd-debug.sock', Path(str(legacy) + '.ttrpc'))
    endpoint = base / 'c' if any(len(os.fsencode(x)) >= 108 for x in managed) else legacy
    return {'root': root, 'base': base, 'state': state,
            'data': base / 'containerd-owned', 'socket': endpoint,
            'config': base / 'containerd.toml', 'log': base / 'containerd.log',
            'owner': base / 'OWNED_CONTAINERD275.json'}


def private_socket_endpoints(root):
    paths = owned_paths(root)
    debug = (paths['base'] / 'cd' if paths['socket'] == paths['base'] / 'c'
             else paths['state'] / 'containerd-debug.sock')
    return (str(paths['base'] / 's'), str(paths['socket']), str(debug),
            str(paths['socket']) + '.ttrpc')


def bind_private_socket_guard(module, original_source):
    """Use the original root/path guard with ONLY its unused managed endpoints
    replaced by the endpoints of this already-required owned-containerd adapter.
    Archive/config/layers/image-ID/resource/lifecycle checks remain untouched.
    """
    import ast
    tree = ast.parse(original_source)
    fn = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'private_paths')
    source = ast.get_source_segment(original_source, fn)
    before = 'for endpoint in (str(paths["socket"]),\n                     str(paths["exec"]) + "/containerd/containerd.sock",\n                     str(paths["exec"]) + "/containerd/containerd-debug.sock",\n                     str(paths["exec"]) + "/containerd/containerd.sock.ttrpc"):'
    need(source.count(before) == 1, 'original private socket guard seam changed')
    source = source.replace(before, 'for endpoint in RECOVERY_SOCKET_ENDPOINTS(root):')
    module.__dict__['RECOVERY_SOCKET_ENDPOINTS'] = private_socket_endpoints
    exec(compile(source, str(Path(__file__).resolve()) + ':socket-path-only', 'exec'), module.__dict__)
    need(module.main.__globals__['private_paths'] is module.private_paths,
         'original main must use the actual endpoint guard')


def containerd_config(paths):
    """No host config imports, CRI services, default sockets, or shared stores."""
    return ('version = 3\nroot = ' + json.dumps(str(paths['data']))
            + '\nstate = ' + json.dumps(str(paths['state']))
            + '\ndisabled_plugins = ["io.containerd.cri.v1.images", '
              '"io.containerd.cri.v1.runtime", "io.containerd.grpc.v1.cri"]\n'
            + '[grpc]\naddress = ' + json.dumps(str(paths['socket']))
            + '\n[debug]\n'
            + ('address = ' + json.dumps(str(paths['base'] / 'cd')) + '\n'
               if paths['socket'] == paths['base'] / 'c' else '')
            + 'level = "info"\n')


def require_private_info(info, paths):
    need(info.get('Containerd', {}).get('Address') == str(paths['socket']),
         'shared or foreign containerd is not accepted')
    need(info.get('Driver') == 'overlay2'
         and Path(info.get('DockerRootDir', '/')) == paths['base'] / 'd',
         'foreign Docker graph store')


def stop_owned_containerd(root, *, require_empty_leases=True, force=False):
    root = check_root(root)
    paths = owned_paths(root)
    if not paths['owner'].exists():
        return {'containerd': 'not_started', 'shared_containerd_signalled': False}
    need(paths['owner'].is_file() and not paths['owner'].is_symlink(), 'foreign owner file')
    owner = json.loads(paths['owner'].read_text())
    need(owner['root'] == str(root) and owner['socket'] == str(paths['socket'])
         and owner['config'] == str(paths['config']), 'foreign containerd owner')
    need(hashlib.sha256(paths['config'].read_bytes()).hexdigest() == owner['config_sha256'],
         'containerd config changed')
    gate = root.parent.parent / 'node-global-gate/reservations.json'
    if require_empty_leases and gate.exists():
        need(json.loads(gate.read_text())['leases'] == {}, 'original resource leases remain')
    current = proc(owner['pid'])
    if current is None or current['state'] in ('Z', 'X', 'x'):
        return {'containerd': 'already_gone', 'pid': owner['pid'], 'shared_containerd_signalled': False}
    need(current['start'] == owner['start'] and current['argv'] == owner['command']
         and current['pgid'] == owner['pid'], 'containerd process identity changed')
    os.killpg(owner['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = proc(owner['pid'])
        if current is None or current['state'] in ('Z', 'X', 'x'):
            return {'containerd': 'stopped', 'pid': owner['pid'], 'shared_containerd_signalled': False}
        need(current['start'] == owner['start'], 'containerd PID reused during stop')
        time.sleep(.1)
    if force:
        # Only preparation failure uses this branch; never force a native leased task.
        os.killpg(owner['pid'], signal.SIGKILL)
        return {'containerd': 'owned_SIGKILL_requested', 'pid': owner['pid'],
                'shared_containerd_signalled': False, 'termination_still_requires_reap': True}
    raise RuntimeError('owned containerd did not terminate; preserve evidence')


class OwnedContainerd:
    def __init__(self, root):
        self.paths = owned_paths(root)
        self.child = None
        self.identity = None
        self.log = None

    def start(self):
        p = self.paths
        need(p['base'].is_dir() and not p['owner'].exists() and not p['state'].exists()
             and not p['data'].exists() and not p['config'].exists(),
             'fresh owned containerd directories required; never replace a prior store')
        need(not p['base'].is_symlink() and (p['root'] / 'x').is_dir()
             and not (p['root'] / 'x').is_symlink(), 'foreign containerd parent')
        need(all(len(os.fsencode(x)) < 108 for x in private_socket_endpoints(p['root'])),
             'containerd socket path too long')
        p['data'].mkdir(mode=0o700)
        p['state'].mkdir(mode=0o700)
        with p['config'].open('x') as stream:
            stream.write(containerd_config(p)); stream.flush(); os.fsync(stream.fileno())
        env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(p['base']),
               'TMPDIR': str(p['base'] / 'tmp'), 'GOMEMLIMIT': '768MiB'}
        argv = ['/usr/bin/containerd', '--config', str(p['config'])]
        self.log = p['log'].open('xb')
        self.child = subprocess.Popen(argv, env=env, stdout=self.log,
                                      stderr=subprocess.STDOUT, start_new_session=True)
        identity = proc(self.child.pid)
        need(identity is not None, 'containerd exited before owner registration')
        self.identity = identity
        save(p['owner'], {'schema': 'reviewbench-owned-containerd275/v1',
                         'root': str(p['root']), 'pid': self.child.pid, 'start': identity['start'],
                         'command': argv, 'socket': str(p['socket']), 'config': str(p['config']),
                         'config_sha256': hashlib.sha256(p['config'].read_bytes()).hexdigest(),
                         'shared_containerd_used': False})
        deadline = time.monotonic() + 45
        while not p['socket'].exists():
            need(self.child.poll() is None and time.monotonic() < deadline,
                 'owned containerd did not create socket')
            need(p['log'].stat().st_size <= MAX_LOG, 'containerd startup log bound')
            time.sleep(.1)
        need(stat.S_ISSOCK(p['socket'].lstat().st_mode), 'containerd endpoint not a socket')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3); sock.connect(str(p['socket']))
            pid, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        need(pid == self.child.pid and uid == 0, 'containerd socket is not owned by the launched root child')
        return p

    def stop(self):
        try:
            if self.child is None:
                return
            if self.paths['owner'].exists():
                owner = json.loads(self.paths['owner'].read_text())
                need(self.identity is not None and owner['pid'] == self.child.pid == self.identity['pid']
                     and owner['start'] == self.identity['start']
                     and owner['command'] == self.identity['argv'],
                     'preparation cleanup owner is not the held child')
                stopped = stop_owned_containerd(self.paths['root'], require_empty_leases=False, force=True)
            else:
                # Popen returned this unreaped child; no PID selection from an untrusted record.
                if self.child.poll() is None:
                    self.child.terminate()
                stopped = {'containerd': 'unregistered_child_terminated'}
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill(); self.child.wait(timeout=10)
            save(self.paths['base'] / 'CONTAINERD_STOPPED275.json',
                 {**stopped, 'exit_status': self.child.returncode, 'data_preserved': True})
        finally:
            if self.log is not None:
                self.log.close()


def load_original(path):
    path = Path(path)
    need(path.name == 'image_identity_probe.py' and path.resolve() == path and not path.is_symlink(),
         'exact original image probe path required')
    raw = path.read_bytes()
    need(len(raw) == ORIGINAL_BYTES and hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA,
         'original image probe bytes changed')
    spec = importlib.util.spec_from_file_location('original_image_probe275', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    bind_private_socket_guard(module, raw.decode())
    # Exactly three frozen functions, in this authenticated probe's PRIVATE
    # namespace. main/shared/private inspect consumers resolve these real globals;
    # original probe/PLAN hashes and all other code remain unchanged on disk.
    need(hashlib.sha256(CONFIG_PROJECTION_SOURCE284.encode()).hexdigest() == CONFIG_PROJECTION_SOURCE_SHA284,
         'Config projection overlay bytes changed285')
    exec(compile(CONFIG_PROJECTION_SOURCE284, str(Path(__file__).resolve()) + ':projection284', 'exec'), module.__dict__)
    need(module.main.__globals__['check_config278'] is module.check_config278
         and module.main.__globals__['verify_inspect'] is module.verify_inspect
         and module.check_config278.__globals__['same_config278'] is module.same_config278,
         'original image consumers disconnected285')
    return module


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--original-probe', type=Path, required=True)
    args, remaining = parser.parse_known_args()
    root_parser = argparse.ArgumentParser(add_help=False)
    root_parser.add_argument('--root', type=Path, required=True)
    selected, _ = root_parser.parse_known_args(remaining)
    root = check_root(selected.root)
    need(args.original_probe == root / 'image_identity_probe.py', 'original root/probe mismatch')
    module = load_original(args.original_probe)
    owned = OwnedContainerd(root)
    old_config, old_run = module.daemon_config, module.Commands.run
    def daemon_config(paths):
        need(paths == module.private_paths(root), 'foreign original daemon paths')
        extra = owned.start()
        return {**old_config(paths), 'containerd': str(extra['socket']), 'log-level': 'info'}
    def command_run(instance, argv, **kwargs):
        result = old_run(instance, argv, **kwargs)
        if owned.child is not None:
            need(owned.child.poll() is None, 'owned containerd exited during image probe')
            need(owned.paths['log'].stat().st_size <= MAX_LOG, 'containerd image-probe log bound')
        if owned.child is not None and len(argv) >= 4 and argv[1:3] == [
                '--host', 'unix://' + str(owned.paths['base'] / 's')] and argv[3] == 'info':
            require_private_info(json.loads(result['stdout']), owned.paths)
        return result
    module.daemon_config, module.Commands.run = daemon_config, command_run
    keep = False
    try:
        sys.argv = [str(args.original_probe), *remaining]
        module.main()  # exact original scan/load/strict inspect/save/oracle/resource behavior
        if owned.child is not None:
            value = json.loads((owned.paths['base'] / 'RESULT.json').read_text())
            keep = value.get('store') == 'private_classic_strictly_verified' and value.get('keep_daemon_requested') is True
            need(owned.child.poll() is None, 'owned containerd exited after strict image probe')
    finally:
        if not keep:
            owned.stop()
        elif owned.log is not None:
            owned.log.close()  # child retains its own fd until original native lifecycle cleanup


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'event': 'owned_image_probe275_failed', 'error': str(error),
                          'native_authorized': False, 'model_posts': 0, 'judge_calls': 0}), file=sys.stderr)
        raise SystemExit(1)
