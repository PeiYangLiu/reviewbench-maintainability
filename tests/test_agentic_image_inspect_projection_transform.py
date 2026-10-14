from pathlib import Path
import ast,base64,copy,hashlib,json,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import agentic_image_inspect_projection_transform as T

def source():
 text=(Path(T.__file__).parent/'agentic_templates/image_wrapper.py').read_text()
 return T.assignments(text)['CONFIG_PROJECTION_SOURCE284']

def module():
 def require(value,why):
  if not value:raise ValueError(why)
 env={'require':require,'json':json,'base64':base64,'digest':lambda b:hashlib.sha256(b).hexdigest()}
 exec(T.project_source(source()),env);return env

@pytest.mark.parametrize('field,value',[('AttachStderr',True),('AttachStdin',True),('AttachStdout',True),('Hostname','old-build-container'),('Domainname','old-build-domain')])
def test_exact_typed_legacy_container_omission_matches(field,value):
 m=module();a={field:value,'Cmd':['original']};b={'Cmd':['original']};old=copy.deepcopy(a)
 assert m['same_config278'](a,b,None) and m['same_config278'](b,a,None);assert a==old

def test_nondefault_field_present_on_both_sides_is_not_dropped():
 m=module()
 assert not m['same_config278']({'Hostname':'one'},{'Hostname':'two'},None)
 assert not m['same_config278']({'AttachStdout':True},{'AttachStdout':False},None)

@pytest.mark.parametrize('field,value',[('AttachStdout',1),('AttachStdin','true'),('Hostname',False),('Domainname',None)])
def test_wrong_legacy_type_is_not_silently_normalized(field,value):
 with pytest.raises(ValueError,match='legacy inspect field type'):module()['same_config278']({field:value},{},None)

@pytest.mark.parametrize('field,value',[('Cmd',['changed']),('Env',['SECRET=changed']),('Entrypoint',['/wrong']),('User','root'),('WorkingDir','/wrong'),('UnknownRuntimeField',True)])
def test_meaningful_and_unknown_settings_stay_exact(field,value):
 assert not module()['same_config278']({field:value},{},None)

def fixture():
 raw=json.dumps({'config':{'AttachStdout':True,'Hostname':'old','Cmd':['original']},'rootfs':{'type':'layers','diff_ids':['sha256:one','sha256:two']},'architecture':'amd64','os':'linux'}).encode();sha=hashlib.sha256(raw).hexdigest();m=module();m.update(IMAGE='sha256:'+sha,ARCHIVE_SHA='archive',ARCHIVE_BYTES=123)
 scan={'config_raw_b64':base64.b64encode(raw).decode(),'config_sha256':sha,'config':json.loads(raw),'rootfs_diff_ids':['sha256:one','sha256:two']};packet={'schema':'reviewbench-original-image-digest-expectation278/v1','image_id':'sha256:'+sha,'config_sha256':sha,'archive_sha256':'archive','archive_bytes':123,'source_inspect':{'Config':{'Cmd':['original']},'RootFS':{'Layers':['sha256:one','sha256:two']},'Architecture':'amd64','Os':'linux'}}
 return m,scan,packet

def test_raw_config_sha_layers_platform_remain_authenticated():
 m,scan,packet=fixture();assert m['check_config278'](scan,packet)
 for change in ['raw','layer','layer-order','architecture','os','cmd']:
  s=copy.deepcopy(scan);p=copy.deepcopy(packet)
  if change=='raw':s['config_raw_b64']=base64.b64encode(b'{}').decode()
  elif change=='layer':s['rootfs_diff_ids'][0]='sha256:wrong'
  elif change=='layer-order':p['source_inspect']['RootFS']['Layers'].reverse()
  elif change=='architecture':p['source_inspect']['Architecture']='arm64'
  elif change=='os':p['source_inspect']['Os']='windows'
  elif change=='cmd':p['source_inspect']['Config']['Cmd']=['changed']
  with pytest.raises(ValueError):m['check_config278'](s,p)

def test_only_exact_original_projection_can_be_transformed():
 with pytest.raises(ValueError,match='EXACT_ORIGINAL_CONFIG_PROJECTION'):T.project_source(source()+'\n')

def test_emitted_projection_literal_hash_is_updated():
 raw=(Path(T.__file__).parent/'agentic_templates/image_wrapper.py').read_bytes();out=T.image(raw);values=T.assignments(out.decode());assert hashlib.sha256(values['CONFIG_PROJECTION_SOURCE284'].encode()).hexdigest()==values['CONFIG_PROJECTION_SOURCE_SHA284'];assert T.OMISSION in values['CONFIG_PROJECTION_SOURCE284']
