import copy
import pytest
from reviewbench_maintainability.agentic.judge_policy import (NO_GEMINI_SCORE_VERSION as V, policy_from_config, policy_from_judgment, validate_completed_panel)
from reviewbench_maintainability.agentic.reporting import make_report
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash
from reviewbench_maintainability.agentic import PROTOCOL

MODELS=["gpt-4.1", "gpt-5.6-sol"]
def config():return {"models":list(MODELS),"score_version":V}
def parts(votes=(True,True)):
 task={"id":"task"};common=dict(id="task",model="solver",protocol=PROTOCOL,task_fingerprint=canonical_hash(task),patch_sha256="abc")
 g={**common,"status":"generated","apply_ok":True}
 c={**common,"status":"pass","scope_ok":True,"correctness_ok":True,"oracle_replayed":True}
 j={**common,"status":"completed","score_version":V,"judge_policy":policy_from_config(config()),"panel":[dict(judge_model=m,passed=b) for m,b in zip(MODELS,votes)],"maintainability_pass":all(votes)}
 return task,g,c,j

def report(j=None,votes=(True,True)):
 task,g,c,old=parts(votes);return make_report({task["id"]:task},["solver"],[g],[c],[old if j is None else j])

@pytest.mark.parametrize("votes",[(True,True),(True,False),(False,True),(False,False)])
def test_new_version_keeps_unanimity_and_veto(votes):
 rows,r=report(votes=votes);assert r['complete'];assert r['score_version']==V
 assert rows[0]['resolved'] is all(votes);assert rows[0]['votes']==sum(votes)
 assert rows[0]['judge_policy']['models']==MODELS

@pytest.mark.parametrize("c",[{'models':MODELS},{'models':[],'score_version':V},{'models':[MODELS[0]]*2,'score_version':V},{'models':MODELS+['other'],'score_version':V},{'models':[MODELS[0],'gemini-3.6-flash'],'score_version':V},{'models':MODELS,'score_version':'unknown'},{'models':[None,MODELS[1]],'score_version':V}])
def test_invalid_config_never_silently_drops_a_judge(c):
 with pytest.raises(ContractError):policy_from_config(c)

@pytest.mark.parametrize('mutation', ['missing_version','missing_policy','legacy_spoof','missing_member','third_member','duplicate','nonboolean','wrong_order','gemini','wrong_exclusion','wrong_aggregation','unknown_version','stored_verdict'])
def test_negative_panel_rejected(mutation):
 _,_,_,j=parts()
 if mutation=='missing_version':j.pop('score_version')
 elif mutation=='missing_policy':j.pop('judge_policy')
 elif mutation=='legacy_spoof':j.pop('score_version');j.pop('judge_policy')
 elif mutation=='missing_member':j['panel'].pop()
 elif mutation=='third_member':j['panel'].append(dict(judge_model='gemini-3.6-flash',passed=True))
 elif mutation=='duplicate':j['panel'][1]['judge_model']=MODELS[0]
 elif mutation=='nonboolean':j['panel'][0]['passed']=1
 elif mutation=='wrong_order':j['panel'].reverse()
 elif mutation=='gemini':j['judge_policy']['models'][1]='gemini-3.6-flash';j['panel'][1]['judge_model']='gemini-3.6-flash'
 elif mutation=='wrong_exclusion':j['judge_policy']['excluded_models']=[]
 elif mutation=='wrong_aggregation':j['judge_policy']['aggregation']='majority'
 elif mutation=='unknown_version':j['score_version']='unknown'
 elif mutation=='stored_verdict':j['maintainability_pass']=False
 with pytest.raises(ContractError):report(j)

def test_missing_remaining_vote_is_infrastructure_not_zero_or_success():
 _,_,_,j=parts();j['status']='infra_error';j['panel'][1]={'judge_model':MODELS[1],'error':'unavailable'};j['maintainability_pass']=None
 rows,r=report(j);assert not r['complete'];assert rows[0]['resolved'] is None;assert rows[0]['votes'] is None

def test_legacy_three_judge_records_unchanged():
 _,_,_,j=parts();j.pop('judge_policy');j.pop('score_version');j['panel'].append(dict(judge_model='gemini-3.6-flash',passed=True))
 rows,r=report(j);assert r['complete'];assert rows[0]['votes']==3;assert 'score_version' not in r

def test_versions_cannot_mix_even_across_incomplete_measurements():
 task,g,c,j=parts();old=copy.deepcopy(j);old['model']='other';old.pop('score_version');old.pop('judge_policy');old['panel'].append(dict(judge_model='gemini-3.6-flash',passed=True))
 with pytest.raises(ContractError,match='mix scoring versions'):
  make_report({'task':task},['solver','other'],[g],[c],[j,old])

def test_new_empty_board_keeps_version():
 rows,r=make_report({'task':{'id':'task'}},['solver'],[],[],[],judge_policy=policy_from_config(config()))
 assert not r['complete'];assert r['score_version']==V;assert rows[0]['score_version']==V

def test_different_two_judge_rosters_do_not_share_board():
 task,g,c,j=parts();other=copy.deepcopy(j);other['model']='other';other['judge_policy']['models'][1]='different';other['panel'][1]['judge_model']='different'
 with pytest.raises(ContractError,match='mix scoring versions'):
  make_report({'task':task},['solver','other'],[g],[c],[j,other])
