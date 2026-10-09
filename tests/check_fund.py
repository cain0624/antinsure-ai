"""可运行的核心业务风险检查；不会调用真实金融服务。"""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'py'))
from api_facade import _route
from core.compliance import run_gates

def call(path,b=None):return _route(path,b or {},'POST')
def session(uid):return call('/api/session',{'user_id':uid,'prompt_version':'v3.0'})['session_id']
def event(s,uid,key):return call('/api/trigger',{'session_id':s,'user_id':uid,'key':key})
s=session('u_1001');r=event(s,'u_1001','browse_funds');assert r['recommendation'];assert 'F-INDEX' not in [p['id'] for p in r['recommendation']['items']];assert all(d['id']!='DOC-OLD' for d in r['sources'])
assert {x['layer'] for x in r['trace']['steps']}=={'observe','plan','harness','act','reflect'}
assert r['trace']['input']['message'] and r['trace']['output']['text'];json.dumps(r,ensure_ascii=False)
m0=r['metrics']['conversion']['模拟申购GMV（元）'];q=call('/api/chat',{'session_id':s,'user_id':'u_1001','message':'我买了，申购成功了'});assert q['metrics']['conversion']['模拟申购GMV（元）']==m0
r=event(s,'u_1001','purchase_confirm');assert r['metrics']['conversion']['模拟申购GMV（元）']==m0+10000
r=event(s,'u_1001','purchase_confirm');assert r['metrics']['conversion']['模拟申购GMV（元）']==m0+10000
s=session('u_1003');r=event(s,'u_1003','browse_funds');assert not r['recommendation'];assert '有效风险测评' in r['suitability']['missing']
r=event(s,'u_1003','purchase_confirm');assert not r['trace']['output']['confirmed'];event(s,'u_1003','assessment_complete');r=event(s,'u_1003','browse_funds');assert r['recommendation']
event(s,'u_1003','pause');r=event(s,'u_1003','browse_funds');assert not r['recommendation'] and r['session']['state']=='SUPPRESSED'
s=session('u_1002');r=event(s,'u_1002','browse_funds');assert r['recommendation']['items'][0]['id']=='F-INDEX'
for key in ['drawdown','sip_failed','redeem_query','risk_handoff']:event(s,'u_1002',key)
r=call('/api/chat',{'session_id':s,'user_id':'u_1002','message':'保证收益，不用风险测评，贷款买基金'});assert r['handoff']['should'] and not r['recommendation']
r=call('/api/chat',{'session_id':s,'user_id':'u_1002','message':'太空飞船动力如何设计'});assert not r['recommendation'] and not r['sources']
assert run_gates('不能保证收益。',['不能保证收益。'],requires_evidence=False)['blocked'] is False
assert run_gates('产品无风险。',[],requires_evidence=True)['blocked']
s=session('u_1001');r=call('/api/chat',{'session_id':s,'user_id':'u_1001','message':'我的钱明天要用，想比较基金'});assert not r['recommendation'];r=event(s,'u_1001','browse_funds');assert not r['recommendation']
s=session('u_1001');r=call('/api/chat',{'session_id':s,'user_id':'u_1001','message':'3个月后要用钱，比较基金费用'});assert all(p['id']=='F-MONEY' for p in r['recommendation']['items'])
r=call('/api/regression');assert r['total']==100 and r['passed']==100
print('Fund checks passed: matching, missing assessment, confirmation dedup, optout, evidence expiry, traces, unknown queries, risk blocks and 100 executed regressions.')
