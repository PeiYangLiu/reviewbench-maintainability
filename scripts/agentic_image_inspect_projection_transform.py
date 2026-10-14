"""Retain image digest/layers/platform while matching Docker's inspect projection.

Docker omits legacy container attachment/hostname fields from image inspect,
including nondefault values preserved in a committed image config blob. Those
bytes remain authenticated by the original image/config SHA. Only omission of
these exact typed container-only fields is normalized; fields present on both
sides, runtime image settings and unknown fields still have to agree.
"""
from pathlib import Path
import ast,hashlib,importlib.util
BASE=Path(__file__).resolve().parent
PREVIOUS_SHA='140a2e4a8a0011f5bd14affba9f41d193d5b6c405d5577ed5482d48429003a9c'
NODE_TEMPLATE_SHA='c20404553bc0bf125b0033dc7495da9aebf884fae38dc8cb63cc1488ccf241de'
ORIGINAL_PROJECTION_SHA='5be2859705b87558149506b1a6049c7017b88e32197a4c28cd923cada5718414'
SEAM='    defaults = {"AttachStderr": False, "AttachStdin": False, "AttachStdout": False,\n'
OMISSION='''    # Docker's image-inspect API omits container-only legacy fields. Raw
    # config SHA, ordered diff IDs and platform were checked by the caller;
    # do not mistake an API representation difference for a different image.
    actual, expected = dict(actual), dict(expected)
    container_only = {"AttachStderr": bool, "AttachStdin": bool, "AttachStdout": bool,
                      "Hostname": str, "Domainname": str}
    for field, value_type in container_only.items():
        if (field in actual) != (field in expected):
            side = actual if field in actual else expected
            require(type(side[field]) is value_type, "legacy inspect field type: " + field)
            side.pop(field)
'''

def need(ok,why):
 if not ok:raise ValueError(why)
def load(name,filename,sha):
 p=BASE/filename;need(hashlib.sha256(p.read_bytes()).hexdigest()==sha,'EXACT_PREVIOUS_PROJECTION_DEPENDENCY')
 s=importlib.util.spec_from_file_location(name,p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def project_source(source):
 need(hashlib.sha256(source.encode()).hexdigest()==ORIGINAL_PROJECTION_SHA and source.count(SEAM)==1,'EXACT_ORIGINAL_CONFIG_PROJECTION')
 result=source.replace(SEAM,OMISSION+SEAM,1);ast.parse(result,feature_version=(3,8));return result

def assignments(text):
 return {n.targets[0].id:ast.literal_eval(n.value) for n in ast.parse(text).body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ('CONFIG_PROJECTION_SOURCE284','CONFIG_PROJECTION_SOURCE_SHA284','IMAGE_SHA275','IMAGE_BYTES275')}

def assignment(text,name,value):
 nodes=[n for n in ast.parse(text).body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id==name];need(len(nodes)==1,'EXACT_PROJECTION_ASSIGNMENT:'+name)
 n=nodes[0];lines=text.splitlines(True);lines[n.lineno-1:n.end_lineno]=[name+' = '+repr(value)+'\n'];return ''.join(lines)

def image(raw):
 text=raw.decode();v=assignments(text);source=v['CONFIG_PROJECTION_SOURCE284']
 need(hashlib.sha256(source.encode()).hexdigest()==v['CONFIG_PROJECTION_SOURCE_SHA284'],'ORIGINAL_PROJECTION_LITERAL_PIN')
 replacement=project_source(source);text=assignment(text,'CONFIG_PROJECTION_SOURCE284',replacement);text=assignment(text,'CONFIG_PROJECTION_SOURCE_SHA284',hashlib.sha256(replacement.encode()).hexdigest());ast.parse(text,feature_version=(3,8));return text.encode()

def configure(policy):
 previous=load('_inspect_previous_runtime','agentic_runtime_identity_transform.py',PREVIOUS_SHA).configure(policy)
 # The reviewed image template binds each task's ORIGINAL_SHA/ORIGINAL_BYTES;
 # no per-task code hash allowlist or image identity exception is introduced.
 template=load('_inspect_original_template','agentic_node_transform.py',NODE_TEMPLATE_SHA)
 def transform(name,raw,descriptor):
  result=previous(name,raw,descriptor)
  if name not in ('IMAGE286.py','ADAPTER286.py'):return result
  normalized,_=template.images(descriptor);before=previous('IMAGE286.py',normalized,descriptor);after=image(before)
  if name=='IMAGE286.py':
   need(result==before,'EXACT_REVIEWED_IMAGE_PROGRAM_BEFORE_PROJECTION');return after
  v=assignments(result.decode());need(v['IMAGE_SHA275']==hashlib.sha256(before).hexdigest() and v['IMAGE_BYTES275']==len(before),'ORIGINAL_IMAGE_CLEANUP_PIN_BEFORE_PROJECTION')
  text=assignment(result.decode(),'IMAGE_SHA275',hashlib.sha256(after).hexdigest());text=assignment(text,'IMAGE_BYTES275',len(after));return text.encode()
 return transform
