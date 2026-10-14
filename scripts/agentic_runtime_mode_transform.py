"""Type-specific Docker runtime socket modes, after the pinned namespace adapter.

Cloud lstat evidence shows metrics.sock and extensions/callback.sock mode0777
inside exact-owned private0700 directories. Such socket access is constrained by
those parent directories; ordinary files/directories keep the original policy.
"""
import ast
import hashlib
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCE_SHA = 'dc5a95053cdc70b5be881da3e11c8903d4b8e8cf73da7428e440782bba2d2ac3'


def original():
    path=BASE/'agentic_owned_runtime_namespace.py'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=SOURCE_SHA:
        raise ValueError('MODE_FIX_ORIGINAL_ADAPTER_PIN_DRIFT')
    spec=importlib.util.spec_from_file_location('_reviewed_owned_runtime',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


OLD = '''        _orn_need(st.st_uid == _orn_os.geteuid() and st.st_dev == device
                  and not st.st_mode & 0o022, 'runtime ownership/mode/device')
        kind = _orn_stat.S_IFMT(st.st_mode)'''
NEW = '''        kind = _orn_stat.S_IFMT(st.st_mode)
        relative = str(path.relative_to(endpoint))
        facts = ('runtime entry path=%s uid=%s expected_uid=%s mode=%s device=%s expected_device=%s kind=%s'
                 % (relative, st.st_uid, _orn_os.geteuid(), oct(_orn_stat.S_IMODE(st.st_mode)),
                    st.st_dev, device, kind))
        _orn_need(st.st_uid == _orn_os.geteuid() and st.st_dev == device, facts)
        if st.st_mode & 0o022:
            _orn_need(kind == _orn_stat.S_IFSOCK
                      and relative in ('metrics.sock', 'extensions/callback.sock')
                      and _orn_stat.S_IMODE(st.st_mode) == 0o777 and st.st_nlink == 1,
                      facts)
            # Every traversable parent of the exception remains exact-owned0700.
            # The enclosing proof still authenticates paired reservations,
            # current daemon PID/start/argv/namespaces and rejects foreign refs.
            _orn_dir(endpoint, device, private=True)
            _orn_dir(path.parent, device, private=True)'''


def runtime_source():
    text=original().RUNTIME_SOURCE
    if text.count(OLD)!=1:
        raise ValueError('MODE_FIX_EXACT_TREE_PREDICATE_REQUIRED')
    return text.replace(OLD,NEW,1)


def transform(filename,raw,descriptor):
    if filename!='ADAPTER286.py':return raw
    text=raw.decode();tree=ast.parse(text)
    rows=[n for n in tree.body if isinstance(n,ast.Assign) and len(n.targets)==1
          and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='_ORN_RUNTIME_SOURCE']
    if len(rows)!=1 or ast.literal_eval(rows[0].value)!=original().RUNTIME_SOURCE:
        raise ValueError('MODE_FIX_REQUIRES_ORIGINAL_NAMESPACE_ADAPTER')
    n=rows[0];lines=text.splitlines(True)
    lines[n.lineno-1:n.end_lineno]=['_ORN_RUNTIME_SOURCE = '+repr(runtime_source())+'\n']
    result=''.join(lines);ast.parse(result)
    return result.encode()
