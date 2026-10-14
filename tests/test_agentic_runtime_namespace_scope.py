"""Task-scoped ownership, including reused-node coexistence regressions."""
import ast
import copy
import json
from pathlib import Path
import sys
import pytest

P=Path(__file__).resolve().parents[1];sys.path.insert(0,str(P/'scripts'))
import agentic_runtime_namespace_scope_transform as T
import test_agentic_owned_runtime_namespace as legacy
from test_agentic_owned_runtime_namespace import owned, original, proc_entry, survey, clean_observation


def bind(owned):
 source=T.runtime_source();scope={'__file__':T.__file__,'_ORN_RUNTIME_SOURCE':source};exec(compile(source,T.__file__+':runtime','exec'),scope);scope['_ORN_PROC']=owned.proc;owned.scope=scope;return scope

@pytest.mark.parametrize('kind',['retained-task','unknown-name','symlink','many-tasks'])
def test_current_task_not_blocked_by_unrelated_directory_entries(owned,kind,monkeypatch):
 scope=bind(owned);before=scope['_orn_proof'](owned.shared,owned.root);parent=owned.endpoint.parent
 siblings=[]
 for i in range(5 if kind=='many-tasks' else 1):
  sibling=parent/('unrelated-'+str(i));siblings.append(sibling)
  if kind=='symlink':sibling.symlink_to('/not/a/current/task')
  else:
   sibling.mkdir(mode=0o700)
   if kind!='unknown-name':(sibling/'OWNER.json').write_text('{"other_task":"retained"}\n')
 # No sibling is traversed; accepting current ownership never means adopting it.
 old_lstat=Path.lstat
 def no_foreign_access(path,*args,**kwargs):
  if any(path==s or path.is_relative_to(s) for s in siblings):raise AssertionError('other task inspected')
  return old_lstat(path,*args,**kwargs)
 monkeypatch.setattr(Path,'lstat',no_foreign_access)
 assert scope['_orn_proof'](owned.shared,owned.root)==before


def test_original_veto_reproduces_on_same_valid_current_task(owned):
 (owned.endpoint.parent/'another-task').mkdir()
 with pytest.raises(ValueError,match='unexpected exec-root sibling'):owned.scope['_orn_proof'](owned.shared,owned.root)
 assert bind(owned)['_orn_proof'](owned.shared,owned.root)['pid']==100


def test_short_current_path_coexists_with_compact_other_task(owned,original):
 scope=bind(owned);owned.endpoint.rename(owned.endpoint.with_name('other-retained-task'))
 (owned.root/'ii/NATIVE_EXEC_ROOT.json').unlink()
 legacy.dump(owned.root/'ii/daemon.json',{**owned.config,'exec-root':str(owned.root/'x')})
 assert scope['_orn_proof'](owned.shared,owned.root) is None
 observer=scope['_orn_observer'](original.observe,original.kit,original.plan)
 assert observer.legacy_roots(owned.shared,owned.root)==([],[])


DAMAGES=['other-hash','base-symlink','endpoint-symlink','internal-symlink','owner-symlink','owner-hardlink','owner-mode','root-mode','base-mode','endpoint-mode','missing-owner','missing-mirror','mirror-drift','native-authorized','config-exec','config-data','config-host','foreign-containerd','duplicate-config-key','pidfile','owner-root','owner-pid-bool','owner-paths','owner-command','dead-daemon','pid-start','pgrp','live-command','foreign-exe','foreign-net']
@pytest.mark.parametrize('damage',DAMAGES)
def test_current_owner_and_process_negatives_still_rejected(original,owned,damage):
 bind(owned);legacy.test_namespace_ownership_negatives_fail_closed(original,owned,damage)

@pytest.mark.parametrize('foreign_kind',['current-exec','other-exec','legacy-root','prefix-collision'])
def test_foreign_process_references_are_not_hidden(original,owned,foreign_kind):
 bind(owned);legacy.test_foreign_processes_not_whitelisted(original,owned,foreign_kind)


def test_original_gate_resource_and_live_container_checks_unchanged(original,owned):
 bind(owned);legacy.test_original_gate_resource_and_live_container_checks_remain(original,owned)


def test_unknown_workload_namespace_still_reaches_original_guard(original,owned):
 bind(owned);legacy.test_unknown_namespace_sibling_still_hits_original_guard(original,owned)


def test_original_observer_accepts_reused_node_without_changing_other_task(owned,original):
 bind(owned);sibling=owned.endpoint.parent/'retained-other-task';sibling.mkdir();(sibling/'marker').write_text('preserve')
 observer=owned.scope['_orn_observer'](original.observe,original.kit,original.plan)
 rows,unknown=observer.legacy_roots(owned.shared,owned.root);processes=survey(original,owned)
 assert rows==unknown==processes['unknown']==processes['legacy_references']==[]
 assert observer.clean_fact(clean_observation(owned,rows,unknown,processes),original.plan['node_resource_limits'])=='initialize_verified_virgin'
 assert (sibling/'marker').read_text()=='preserve'


def test_composition_changes_only_runtime_source_literal_and_leaves_image_pins():
 node=P/'.benchmarks/framework-repair-20261012-v1/integration/production-live/native-waves/0008/cohort/lanes/3a94e065416ada7d60bb/node'
 if not node.exists():pytest.skip('optional deployed carrier not present')
 raw=(node/'ADAPTER286.py').read_bytes();new=T.apply_scope('ADAPTER286.py',raw,{})
 old_tree=ast.parse(raw);new_tree=ast.parse(new)
 def without_source(tree):return ast.dump(ast.Module(body=[n for n in tree.body if not(isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='_ORN_RUNTIME_SOURCE')],type_ignores=[]))
 assert without_source(old_tree)==without_source(new_tree)
 assert (node/'ADAPTER286.py').read_bytes()==raw
 with pytest.raises(ValueError,match='PREVIOUS_REVIEWED'):T.apply_scope('ADAPTER286.py',new,{})
