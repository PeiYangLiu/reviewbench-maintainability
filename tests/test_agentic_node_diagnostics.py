"""Failure-injection tests: useful diagnostics with no credential/local leakage."""
import ast
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import agentic_diagnostic_node_transform as T


def helper():
    module=types.ModuleType('test_node_diagnostics')
    exec(compile((SCRIPTS/'agentic_templates/node_diagnostics.py').read_bytes(),'node_diagnostics.py','exec'),module.__dict__)
    return module


def test_safe_exception_retains_stage_message_frames_not_locals(tmp_path):
    m=helper();m._rbdiag_begin(tmp_path,'prepare.resource_check')
    secret='a-real-looking-fixture-key-1234567';m._rbdiag_remember(secret)
    try:
        private_local={'password':secret}
        raise RuntimeError('actual node CPU pressure; api_key='+secret+' URL https://user:password@host/path?sig=abc')
    except RuntimeError as exc:
        value=m._rbdiag_exception(exc)
    text=json.dumps(value)
    assert secret not in text and 'user:password' not in text and '?sig=' not in text
    assert 'actual node CPU pressure' in value['message']
    assert value['stage']=='prepare.resource_check' and value['frames'][-1]['function']==test_safe_exception_retains_stage_message_frames_not_locals.__name__
    assert value['frames'][-1]['line']>0 and value['locals_logged'] is False
    assert 'private_local' not in text
    assert any('exception' in f.name for f in (tmp_path/'diagnostics-runtime').iterdir())


def test_resource_snapshot_is_written_even_when_original_guard_raises(tmp_path):
    m=helper();m._rbdiag_begin(tmp_path,'prepare.start')
    def reject(plan,host):raise RuntimeError('actual node CPU pressure')
    scope={'resource_check':reject}
    exec("def observe_resources(kit, plan, root):\n return resource_check(plan, {'host_cpus':4,'load_1m':99.0})",scope)
    runtime=types.SimpleNamespace(observe_resources=scope['observe_resources'])
    plan={'node_resource_limits':{'cpus':4},'resource_request':{'cpus':2}}
    with pytest.raises(RuntimeError,match='actual node CPU pressure'):
        m._rbdiag_resources(runtime,None,plan,tmp_path)
    assert scope['resource_check'] is reject
    f=next((tmp_path/'diagnostics-runtime').glob('*RESOURCE_OBSERVATION_BEFORE_CHECK.json'))
    data=json.loads(f.read_text());assert data['host']['load_1m']==99 and data['request']==plan['resource_request']


def test_node_snapshot_precedes_original_clean_fact_failure(tmp_path):
    m=helper();m._rbdiag_begin(tmp_path,'prepare.start')
    observation={'unknown_namespace':[{'path':'.exec/test','reason':'legacy identity config absent/unsafe'}]}
    def capture(*a,**kw):return observation
    observer=types.SimpleNamespace(capture=capture)
    def reused(*a):return None,observer
    scope={'reused':reused}
    exec("def node_clean(root,plan,kit,env):\n paths,observer=reused(kit,plan)\n value=observer.capture(root,plan,env)\n raise RuntimeError('unknown node/process/daemon facts; no bypass')",scope)
    physical=types.SimpleNamespace(node_clean=scope['node_clean'])
    with pytest.raises(RuntimeError,match='unknown node/process'):
        m._rbdiag_node_clean(physical,tmp_path,{},None,{})
    assert scope['reused'] is reused and observer.capture is capture
    f=next((tmp_path/'diagnostics-runtime').glob('*NODE_OBSERVATION_BEFORE_CHECK.json'))
    assert json.loads(f.read_text())==observation


def test_logging_io_failure_never_masks_original_exception(tmp_path,capsys):
    m=helper();m._rbdiag_begin(tmp_path,'prepare.test')
    with patch.object(m._rd_os,'open',side_effect=OSError('no space')):
        try:raise RuntimeError('actual node CPU pressure')
        except RuntimeError as exc:value=m._rbdiag_exception(exc)
    assert value['message']=='actual node CPU pressure'
    assert 'DIAGNOSTIC_WRITE_FAILED' in capsys.readouterr().err


@pytest.mark.parametrize('value',['Bearer abc123','api_key="short-secret"','password=short-secret','https://host/path?sig=short-secret'])
def test_unregistered_common_secret_syntax_is_redacted(value):
    m=helper();text=m._rbdiag_text(value)
    assert 'short-secret' not in text and 'abc123' not in text


def test_control_bytes_hex_base64_repr_are_redacted():
    m=helper();value=bytes(range(32));m._rbdiag_remember(value)
    for text in [value.hex(),repr(value),m._rd_b64.b64encode(value).decode()]:assert text not in m._rbdiag_text(text)


def node_sources():
    root=SCRIPTS.parent/'.benchmarks/framework-repair-20261012-v1/integration/production-live/native-waves/0003/cohort-task-pins-v2/lanes/0e54eb1609360a93be55/node'
    if not root.exists():pytest.skip('optional original carrier evidence not installed')
    return root


def test_actual_frozen_node_and_task_transforms_leave_originals_unchanged():
    root=node_sources()
    for name in ['MODIFIED_FILE.py','TASK286.py']:
        path=root/name;raw=path.read_bytes();changed=T.diagnostic_transform(name,raw,{})
        ast.parse(changed);assert path.read_bytes()==raw and changed!=raw
        assert b'_rbdiag_exception(exc)' in changed
    task=T.diagnostic_transform('TASK286.py',(root/'TASK286.py').read_bytes(),{}).decode()
    for name in ['PREPARATION_RESOURCES.json','ORIGINAL_NODE_OBSERVATION.json','diagnostics-runtime','_rbdiag_upload_failure']:
        assert name in task


def test_actual_upload_failure_does_not_replace_preparation_failure(tmp_path,capsys):
    root=node_sources();raw=T.diagnostic_transform('TASK286.py',(root/'TASK286.py').read_bytes(),{})
    tree=ast.parse(raw);diag=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='diagnostics')
    m=helper();m._rbdiag_begin(tmp_path/'task','task.prepare')
    # The parent task directory normally exists; ensure it exists in the fixture.
    (tmp_path/'task').mkdir(exist_ok=True)
    (tmp_path/'ii').mkdir();(tmp_path/'ii/RESULT.json').write_text('{}')
    uploads=[]
    class Physical:
        @staticmethod
        def put_blob(name,raw):
            uploads.append(name)
            raise OSError('HTTP failure https://host/?sig=secret')
    def need(ok,msg):
        if not ok:raise RuntimeError(msg)
    scope=dict(root=tmp_path,physical=Physical,prefix='fixture',control_hex='control-secret',credential='credential-secret',
               C=types.SimpleNamespace(raw=lambda p,n:Path(p).read_bytes(),need=need,MIB=1024**2),
               _rbdiag_upload_failure=m._rbdiag_upload_failure)
    exec(compile(ast.Module(body=[diag],type_ignores=[]),'actual_task_diagnostics.py','exec'),scope)
    scope['diagnostics']()  # formerly raised, replacing the original child failure
    assert uploads and 'DIAGNOSTIC_UPLOAD_FAILED' in capsys.readouterr().err


def test_changed_seam_fails_closed():
    root=node_sources();raw=(root/'MODIFIED_FILE.py').read_bytes()
    changed=raw.replace(b'R.observe_resources(target, plan, root)',b'unknown_resource_check(target, plan, root)')
    with pytest.raises(ValueError,match='DIAGNOSTIC_EXACT_SEAM_CHANGED'):T.diagnostic_transform('MODIFIED_FILE.py',changed,{})
