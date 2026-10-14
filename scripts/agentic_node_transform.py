"""Apply shared node-runtime fixes while retaining each task's exact probe pin.

Only ORIGINAL_SHA/ORIGINAL_BYTES are template data. All remaining wrapper bytes
must match the reviewed program. This avoids a per-task code-hash allowlist while
not accepting arbitrary code or changing the task's original immutable image.
"""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path

BASE=Path(__file__).resolve().parent/'agentic_templates'
TEMPLATE_SHA='e6177bd10fad07f44c413fe1925abe34ba09cfd5cadf3d05685c1e0f0284c134'
FIX_SHA='71acb3994cebc4fce1d293ae76bcfc1a943f53b16ed7a297c3b7492860e8d0a4'


def load():
    p=BASE/'node_runtime_fixes.py';raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=FIX_SHA:raise ValueError('FIX_PROGRAM_PIN_DRIFT')
    spec=importlib.util.spec_from_file_location('_node_program_fixes',p)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


def assignment(text,name,value):
    nodes=[n for n in ast.parse(text).body if isinstance(n,ast.Assign) and len(n.targets)==1
           and isinstance(n.targets[0],ast.Name) and n.targets[0].id==name]
    if len(nodes)!=1:raise ValueError('EXACT_LITERAL_TEMPLATE_FIELD:'+name)
    n=nodes[0];lines=text.splitlines(True);lines[n.lineno-1:n.end_lineno]=[name+' = '+repr(value)+'\n']
    return ''.join(lines)


def images(descriptor):
    ref=descriptor['canonical_plan'];raw=Path(ref['path']).read_bytes()
    if len(raw)!=ref['bytes'] or hashlib.sha256(raw).hexdigest()!=ref['sha256']:
        raise ValueError('CANONICAL_PLAN_PIN_DRIFT')
    plan=json.loads(raw);probe=plan['files']['reuse/image/image_identity_probe.py']
    template=(BASE/'image_wrapper.py').read_bytes()
    if hashlib.sha256(template).hexdigest()!=TEMPLATE_SHA:raise ValueError('IMAGE_PROGRAM_TEMPLATE_DRIFT')
    def bind(raw):
        text=assignment(raw.decode(),'ORIGINAL_SHA',probe['sha256'])
        return assignment(text,'ORIGINAL_BYTES',probe['bytes']).encode()
    before=bind(template);after=bind(load().transform('IMAGE286.py',template,descriptor))
    return before,after


def transform(file_name,raw_bytes,descriptor):
    if file_name not in ('IMAGE286.py','ADAPTER286.py'):return raw_bytes
    before,after=images(descriptor)
    if file_name=='IMAGE286.py':
        if raw_bytes!=before:raise ValueError('IMAGE_PROGRAM_DIFFERS_BEYOND_PINNED_TEMPLATE_DATA')
        return after
    nodes=[n for n in ast.parse(raw_bytes).body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)]
    values={n.targets[0].id:ast.literal_eval(n.value) for n in nodes if n.targets[0].id in ('IMAGE_SHA275','IMAGE_BYTES275')}
    if values!={'IMAGE_SHA275':hashlib.sha256(before).hexdigest(),'IMAGE_BYTES275':len(before)}:
        raise ValueError('ORIGINAL_IMAGE_CLEANUP_PIN_MISMATCH')
    text=assignment(raw_bytes.decode(),'IMAGE_SHA275',hashlib.sha256(after).hexdigest())
    return assignment(text,'IMAGE_BYTES275',len(after)).encode()
