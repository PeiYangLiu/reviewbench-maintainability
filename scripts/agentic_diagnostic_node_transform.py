"""Compose immutable task-specific runtime fixes and lossless failure diagnostics.

New physical carriers only. The canonical engine, resource decisions, native
attempt ledger and old node artifacts are not edited or executed here.
"""
import ast
import hashlib
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
PINS = {
    'agentic_owned_runtime_namespace.py': 'dc5a95053cdc70b5be881da3e11c8903d4b8e8cf73da7428e440782bba2d2ac3',
    'agentic_templates/node_diagnostics.py': 'c283d819bbbf63f863d136a661edd0de1157c2142b9cd9f6695e143d279439c9',
    'agentic_node_transform.py': 'c20404553bc0bf125b0033dc7495da9aebf884fae38dc8cb63cc1488ccf241de',
}


def load(name):
    path = BASE / name
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINS[name]:
        raise ValueError('DIAGNOSTIC_TRANSFORM_DEPENDENCY_DRIFT:' + name)
    spec = importlib.util.spec_from_file_location('_reviewbench_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('DIAGNOSTIC_EXACT_SEAM_CHANGED:' + old[:100])
    return text.replace(old, new, 1)


def diagnostic_transform(name, raw, descriptor):
    if name not in ('MODIFIED_FILE.py', 'TASK286.py'):
        return raw
    path = BASE / 'agentic_templates/node_diagnostics.py'
    helper = path.read_bytes()
    if hashlib.sha256(helper).hexdigest() != PINS['agentic_templates/node_diagnostics.py']:
        raise ValueError('DIAGNOSTIC_HELPER_PIN_DRIFT')
    text = raw.decode()
    text = replace_once(text, '\nfrom ADAPTER286 import bootstrap\n',
                        '\n' + helper.decode() + '\nfrom ADAPTER286 import bootstrap\n')
    catch = '"exception_type": type(exc).__name__,\n'
    text = replace_once(text, catch, catch + '                          "diagnostic": _rbdiag_exception(exc),\n')
    if name == 'MODIFIED_FILE.py':
        text = replace_once(text, '    root.mkdir(mode=0o700)\n',
                            '    root.mkdir(mode=0o700)\n    _rbdiag_begin(root, "prepare.root_created")\n')
        text = replace_once(text, '    control = physical.read_fd(args.control_fd, 32, True)',
                            '    control = _rbdiag_remember(physical.read_fd(args.control_fd, 32, True))')
        text = replace_once(text, 'physical.read_fd(args.credential_fd, 4096))',
                            '_rbdiag_remember(physical.read_fd(args.credential_fd, 4096)))')
        for seam, stage in [('    physical.command(root, "ORIGINAL_BOOTSTRAP",', 'prepare.bootstrap'),
                            ('    physical.command(root, "ORIGINAL_GO_IMAGE_PROBE",', 'prepare.image_probe'),
                            ('    result = C.read(root / "ii/RESULT.json")', 'prepare.image_result_check'),
                            ('    config = C.read(target / "input/config.json")', 'prepare.node_gate'),
                            ('        C.write_once(root / "ORIGINAL_GIT_READBACK.json",', 'prepare.git_materialization'),
                            ('    C.write_once(root / "MANAGED_NODE_READY.json", ready)', 'prepare.ready')]:
            indent = seam[:len(seam)-len(seam.lstrip())]
            text = replace_once(text, seam, indent + '_rbdiag_phase(' + repr(stage) + ')\n' + seam)
        text = replace_once(text, 'R.observe_resources(target, plan, root)',
                            '_rbdiag_resources(R, target, plan, root)')
        text = replace_once(text, '    paths, observation, action = physical.node_clean(root, plan, target, env)',
                            '    paths, observation, action = _rbdiag_node_clean(physical, root, plan, target, env)')
        text = replace_once(text, '    C.need(kit == root / "managed-kit", "run only the installed immutable kit")',
                            '    _rbdiag_begin(root, "native.authorize")\n    C.need(kit == root / "managed-kit", "run only the installed immutable kit")')
        text = replace_once(text, 'R.observe_resources(kit, plan, root)',
                            '_rbdiag_resources(R, kit, plan, root, "native.resource_check")')
        text = replace_once(text, '    _, observation, action = physical.node_clean(root, plan, kit, env)',
                            '    _, observation, action = _rbdiag_node_clean(physical, root, plan, kit, env, "native.node_clean")')
        text = replace_once(text, '            status = execute_original(root, plan, plan_sha, env)',
                            '            _rbdiag_phase("native.original_controller")\n            status = execute_original(root, plan, plan_sha, env)')
        text = replace_once(text, '    return returned_evidence(root, plan, physical, status, actual)',
                            '    _rbdiag_phase("native.return_evidence")\n    return returned_evidence(root, plan, physical, status, actual)')
    else:
        text = replace_once(text, '    directory = Path.cwd().resolve()',
                            '    directory = Path.cwd().resolve()\n    _rbdiag_begin(directory, "task.bootstrap")')
        text = replace_once(text, 'control_hex = os.environ.pop("RB_DISPATCH_HMAC")',
                            'control_hex = _rbdiag_remember(os.environ.pop("RB_DISPATCH_HMAC"))')
        text = replace_once(text, 'control = bytes.fromhex(control_hex)',
                            'control = _rbdiag_remember(bytes.fromhex(control_hex))')
        text = replace_once(text, 'credential = os.environ.pop("LLM_API_KEY")',
                            'credential = _rbdiag_remember(os.environ.pop("LLM_API_KEY"))')
        text = replace_once(text, '            "NATIVE_OWNER.json", "ORIGINAL_NATIVE_EXIT.json",',
                            '            "PREPARATION_RESOURCES.json", "ORIGINAL_NODE_OBSERVATION.json",\n'
                            '            "RUNTIME_VERIFICATION.json", "RUNTIME_READY.json", "ii/NATIVE_EXEC_ROOT.json",\n'
                            '            "NATIVE_OWNER.json", "ORIGINAL_NATIVE_EXIT.json",')
        text = replace_once(text, '        for path in paths:\n',
                            '        paths += sorted((root / "diagnostics-runtime").glob("*.json"))[:128]\n'
                            '        for path in paths:\n')
        old = '''            if path.exists():
                raw = C.raw(path, 32*C.MIB)
                C.need(control_hex.encode() not in raw and credential.encode() not in raw,
                       "credential echo in original diagnostics")
                physical.put_blob(prefix + "/diagnostics/" + str(path.relative_to(root)), raw)'''
        new = '''            try:
                if path.exists():
                    raw = C.raw(path, 32*C.MIB)
                    C.need(control_hex.encode() not in raw and credential.encode() not in raw,
                           "credential echo in original diagnostics")
                    physical.put_blob(prefix + "/diagnostics/" + str(path.relative_to(root)), raw)
            except Exception as upload_error:
                _rbdiag_upload_failure(str(path.relative_to(root)), upload_error)'''
        text = replace_once(text, old, new)
        text = replace_once(text, '    prep = wait_grant(',
                            '    _rbdiag_phase("task.wait_preparation_grant")\n    prep = wait_grant(')
        text = replace_once(text, '    result = execute(command, control)\n',
                            '    _rbdiag_phase("task.prepare")\n    result = execute(command, control)\n')
        text = replace_once(text, '    auth = wait_grant(',
                            '    _rbdiag_phase("task.wait_native_grant")\n    auth = wait_grant(')
    ast.parse(text)
    return text.encode()


def transform(name, raw, descriptor):
    result = load('agentic_node_transform.py').transform(name, raw, descriptor)
    result = load('agentic_owned_runtime_namespace.py').transform(name, result, descriptor)
    return diagnostic_transform(name, result, descriptor)
