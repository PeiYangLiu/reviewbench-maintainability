import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import pytest
S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
import agentic_runtime_mode_transform as M


def namespace(modified):
    scope={}
    source=M.runtime_source() if modified else M.original().RUNTIME_SOURCE
    exec(compile(source,'owned-runtime-mode-test.py','exec'),scope)
    return scope


@pytest.fixture
def runtime():
    with tempfile.TemporaryDirectory(prefix='rb-mode-',dir='/tmp') as td:
        root=Path(td);root.chmod(0o700);endpoint=root/'runtime';endpoint.mkdir(mode=0o700)
        (endpoint/'extensions').mkdir(mode=0o700)
        sockets=[]
        for rel in ['metrics.sock','extensions/callback.sock']:
            sock=socket.socket(socket.AF_UNIX);sock.bind(str(endpoint/rel));os.chmod(endpoint/rel,0o777);sockets.append(sock)
        (endpoint/'OWNER.json').write_text('{}');(endpoint/'OWNER.json').chmod(0o600)
        yield endpoint
        for sock in sockets:sock.close()


def test_real_kernel_socket_modes_reproduce_and_fix_original_guard(runtime):
    old=namespace(False);new=namespace(True)
    with pytest.raises(ValueError,match='runtime ownership/mode/device'):old['_orn_tree'](runtime,runtime.stat().st_dev)
    new['_orn_tree'](runtime,runtime.stat().st_dev)
    assert stat.S_IMODE((runtime/'metrics.sock').stat().st_mode)==0o777
    assert stat.S_IMODE(runtime.stat().st_mode)==0o700


@pytest.mark.parametrize('change',['world-file','group-file','unsafe-parent','unsafe-root','unrecognized-socket','unexpected-mode','symlink','hardlink','wrong-device','setuid-socket'])
def test_exceptions_never_allow_unowned_or_ordinary_writable_objects(runtime,change):
    ns=namespace(True);device=runtime.stat().st_dev;extra=None
    if change in ['world-file','group-file']:
        f=runtime/'file';f.write_text('fixture');f.chmod(0o666 if change=='world-file' else 0o660)
    elif change=='unsafe-parent':(runtime/'extensions').chmod(0o755)
    elif change=='unsafe-root':runtime.chmod(0o755)
    elif change=='unrecognized-socket':
        extra=socket.socket(socket.AF_UNIX);extra.bind(str(runtime/'foreign.sock'));(runtime/'foreign.sock').chmod(0o777)
    elif change=='unexpected-mode':(runtime/'metrics.sock').chmod(0o776)
    elif change=='setuid-socket':(runtime/'metrics.sock').chmod(0o4777)
    elif change=='symlink':(runtime/'alias').symlink_to(runtime/'OWNER.json')
    elif change=='hardlink':os.link(runtime/'OWNER.json',runtime/'other')
    elif change=='wrong-device':device+=1
    try:
        with pytest.raises(ValueError):ns['_orn_tree'](runtime,device)
    finally:
        if extra:extra.close()


def test_mode_failure_retains_actual_entry_attributes(runtime):
    (runtime/'bad-file').write_text('x');(runtime/'bad-file').chmod(0o666)
    with pytest.raises(ValueError,match=r'path=bad-file.*uid=.*mode=0o666.*device='):
        namespace(True)['_orn_tree'](runtime,runtime.stat().st_dev)


def test_transform_is_copy_only_and_rejects_double_application():
    p=S.parent/'.benchmarks/framework-repair-20261012-v1/integration/production-live/native-waves/0004/cohort/lanes/0e54eb1609360a93be55/node/ADAPTER286.py'
    if not p.exists():pytest.skip('optional original carrier evidence absent')
    before=p.read_bytes();after=M.transform('ADAPTER286.py',before,{})
    assert p.read_bytes()==before and after!=before
    with pytest.raises(ValueError,match='REQUIRES_ORIGINAL'):M.transform('ADAPTER286.py',after,{})
    assert M.transform('TASK286.py',before,{}) is before
