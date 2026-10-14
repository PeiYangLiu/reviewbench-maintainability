"""New-carrier composition: existing repairs + observed socket modes + IMDS cache."""
import hashlib
import importlib.util
from pathlib import Path
BASE=Path(__file__).resolve().parent
PINS={
    'agentic_runtime_mode_transform.py':'491eb9a4ef31bfd4e15d2c030b1ce681bebdf04e8c908eac7086fb68e3e86677',
    'agentic_diagnostic_node_transform.py':'cfe1e3431ea7f765445279dcd207e8561b8923834ef96a623868ec6fa43169a2',
    'agentic_imds_token_cache.py':'a157cf071fddb2ba55a21fa2810f3075b2a59834f5c845cbd31a620164a3c641',
}

def load(name):
    path=BASE/name
    if hashlib.sha256(path.read_bytes()).hexdigest()!=PINS[name]:
        raise ValueError('RUNTIME_TOKEN_DEPENDENCY_PIN_DRIFT:'+name)
    spec=importlib.util.spec_from_file_location('_reviewbench_runtime_token_'+path.stem,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def transform(name,raw,descriptor):
    result=load('agentic_diagnostic_node_transform.py').transform(name,raw,descriptor)
    result=load('agentic_runtime_mode_transform.py').transform(name,result,descriptor)
    return load('agentic_imds_token_cache.py').transform(name,result,descriptor)
