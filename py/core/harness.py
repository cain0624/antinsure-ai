"""AntFund 有限状态编排：授权触达 → 信息确认 → 适当性 → 证据 → 候选 → 四道闸。"""
import os,json,uuid,time,hashlib
from dataclasses import dataclass,field
from .agents import ObserveLayer,Planner,Executors
from .rag import Retriever
from .trace import TraceStore
from .finance import suitability,fee_quote
from .compliance import run_gates
BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_VERSIONS={'v1.0':{'label':'通用开场对照','featured':'合规边界一致，场景表达简化'},'v3.0':{'label':'基金场景版','featured':'风险与期限先过滤，依据可核验，触达可停止'}}
TOOL_REGISTRY={
 'profile.read':{'perm':['profile_agent'],'desc':'读取演示画像与授权状态'},
 'kb.search':{'perm':['rag_agent'],'desc':'版本过滤与词项检索'},
 'suitability.check':{'perm':['suitability_agent'],'desc':'风险、期限、自有资金硬过滤'},
 'fund.compare':{'perm':['recommend_agent'],'desc':'仅比较通过过滤的基金'},
 'script.compose':{'perm':['script_agent'],'desc':'证据约束的话术合成'},
 'handoff.route':{'perm':['handoff_agent'],'desc':'风险升级人工'},
}
ALL_TOOLS=list(TOOL_REGISTRY);STATES=['IDLE','OBSERVING','PLANNING','EXECUTING','GATING','DELIVERED','HANDOFF','SUPPRESSED']
TRIGGER_MESSAGES={'browse_funds':'我浏览了多只基金，想比较投资范围和费用','assessment_stuck':'风险测评没完成，我不知道风险等级是什么意思','assessment_complete':'模拟完成风险测评，结果C2；请解释基金风险','drawdown':'持有基金净值回落，我很担心要不要卖掉','sip_failed':'定投扣款失败，需要如何处理','redeem_query':'赎回费用和资金到账安排是什么','risk_handoff':'我想投诉，让人工解释风险','purchase_confirm':'模拟申购确认：已阅读风险、费用及资料并自主确认','pause':'停止提醒，请不要再主动联系'}
@dataclass
class Session:
    session_id:str
    user_id:str
    prompt_version:str='v3.0'
    state:str='IDLE'
    turn:int=0
    memory:dict=field(default_factory=dict)
class Harness:
    def __init__(self,store):
        def load(name):
            with open(os.path.join(BASE,'data',name),encoding='utf8') as f:return json.load(f)
        self.products=load('products.json')['products'];p=load('profiles.json');self.profiles=p['profiles'];self.personas=p['personas'];self.cases=load('cases.json')['cases'];self.badcases_seed=load('cases.json')['badcases'];self.retriever=Retriever(load('corpus.json')['corpus']);self.observe=ObserveLayer();self.planner=Planner();self.executors=Executors(self.products,self.retriever);self.store=store;store.regression_runner=self.regression
    def bucket(self,sid):return 'v3.0' if int(hashlib.sha256(sid.encode()).hexdigest(),16)%100<80 else 'v1.0'
    def tool_permission(self,agent):return [k for k,v in TOOL_REGISTRY.items() if agent in v['perm']]
    def regression(self):
        cases=[]
        bad=['这只基金稳赚不赔。','保证年化收益。','这个产品保本保收益。','不用风险测评也可以买。','贷款买基金可以翻倍。','全市场最好，绝对安全。','申购手续费只有0.01%。','借钱投资这个产品。']
        good=['基金有风险，投资需谨慎。','过往业绩不代表未来表现。','不能保证收益。','不保本，请核对风险。','请先完成风险测评。']
        # 100条真实规则回归，含措辞变体；不是100个独立业务场景。
        for i in range(60):
            text=['','请注意：','顾问说：','产品介绍：','本次答复：'][i%5]+bad[i%len(bad)]
            got=run_gates(text,['基金有风险，投资需谨慎。'],requires_evidence=False)['blocked'];cases.append({'id':f'GATE-{i+1:03}','expect':'风险或无依据数字必须拦截','pass':got})
        for i in range(20):
            text=good[i%len(good)];got=not run_gates(text,[text],requires_evidence=False)['blocked'];cases.append({'id':f'SAFE-{i+1:03}','expect':'正常风险提示不能误杀','pass':got})
        for i in range(10):
            p=dict(self.profiles['u_1001'],risk_level=1,assessment_valid=i%2==0);m=suitability(p,self.products);ok=all(not x['eligible'] for x in m['rows'] if next(t for t in self.products if t['id']==x['product_id'])['risk_level']>1);cases.append({'id':f'MATCH-{i+1:03}','expect':'超风险候选必须过滤','pass':ok and (i%2==0 or not any(x['eligible'] for x in m['rows']))})
        for i in range(5):
            got=run_gates('产品有明确保障。',[],requires_evidence=True)['blocked'];cases.append({'id':f'EVIDENCE-{i+1}','expect':'无证据不得生成事实','pass':got})
        for i in range(5):
            q=fee_quote(self.products[1],10000);cases.append({'id':f'FEE-{i+1}','expect':'Decimal费用计算正确','pass':q['fee']=='29.91'})
        n=sum(c['pass'] for c in cases);return {'total':len(cases),'passed':n,'verdict':'离线规则回归通过（不等同生产发布审批）' if n==len(cases) else '回归失败，停止发布','cases':cases,'ran_at':time.strftime('%H:%M:%S')}
class Orchestrator:
    def __init__(self,harness,store):self.h=harness;self.store=store;self.sessions={}
    def get_session(self,sid,user_id,prompt_version='v3.0'):
        if user_id not in self.h.profiles:raise ValueError('未知演示画像')
        sid=sid or 'ss_'+uuid.uuid4().hex[:10]
        if sid not in self.sessions:self.sessions[sid]=Session(sid,user_id,self.h.bucket(sid) if prompt_version=='auto' else prompt_version)
        s=self.sessions[sid]
        if s.user_id!=user_id:raise ValueError('会话与用户不一致')
        return s
    def _tool(self,trace,agent,name,fn,*args):
        if name not in self.h.tool_permission(agent):raise PermissionError('工具不在执行器白名单')
        start=time.perf_counter();value=fn(*args);trace.add('act',agent,name,'执行器输出可核验',latency_ms=round((time.perf_counter()-start)*1000,2),input=args,output=value);return value
    def run_turn(self,s,message,behaviors=None):
        behaviors=behaviors or [];s.turn+=1;r=self.store.new_trace(s.session_id,s.user_id);r.prompt_version=s.prompt_version;r.input={'message':message,'behaviors':behaviors,'user_id':s.user_id};p=dict(self.h.profiles[s.user_id]);p.update(s.memory.get('profile_updates',{}));s.state='OBSERVING';obs=self.h.observe.observe(p,s.memory,message,behaviors);r.add('observe','Observe','感知','使用演示画像、行为与会话记忆',input=r.input,output={'profile':p,'behaviors':behaviors});plan=self.h.planner.plan(obs,message,s.state,s.turn);s.state='PLANNING';r.add('plan','Planner','业务策略',plan['strategy'],**plan);r.add('harness','Harness','权限与状态',f'Prompt {s.prompt_version}；交易工具未注册',permissions=TOOL_REGISTRY,input={'state':s.state},output={'turn':s.turn});ex=self.h.executors
        profile=self._tool(r,'profile_agent','profile.read',ex.profile_agent,obs);rag=self._tool(r,'rag_agent','kb.search',ex.rag_agent,message,plan);match=self._tool(r,'suitability_agent','suitability.check',ex.suitability_agent,profile);rec=self._tool(r,'recommend_agent','fund.compare',ex.recommend_agent,profile,match,plan['intent'])
        if not rag['hits']:rec=None
        # 主动触达必须授权，停止后不再推送；确认事件不属于主动推送。
        suppressed=bool(behaviors and behaviors[0] not in ['purchase_confirm','assessment_complete','pause','risk_handoff'] and (s.memory.get('optout') or not profile.get('contact_consent')))
        intent=plan['intent'];confirmed=False
        if intent=='pause':s.memory['optout']=True
        if intent=='confirm':
            last=s.memory.get('last_candidates',[]);approved={x['product_id'] for x in match['rows'] if x['eligible']}
            confirmed=bool(behaviors==['purchase_confirm'] and last and last[0] in approved and not s.memory.get('confirmed'))
        text=self._tool(r,'script_agent','script.compose',ex.script_agent,intent,profile,rag,rec,match,s.prompt_version)
        if intent=='confirm' and not confirmed:text='当前没有通过适当性校验的比较候选，或该会话已确认。请先了解产品资料并自主确认；聊天中的购买意愿不计作申购。'
        if suppressed:text='本次主动触达已停止：无授权或用户已拒绝提醒。不会发送销售内容。'
        evidence=[x['text'] for x in rag['hits']]
        factual=bool(rec or (intent=='qa' and rag['hits'] and not match['missing']))
        if rec:evidence.extend(x['name']+' '+x['fee_rate']+' '+x['scope'] for x in rec['items'])
        gate=run_gates(text,evidence,requires_evidence=factual)
        if intent=='risk':
            raw=run_gates(message,[],requires_evidence=False)
            gate=raw if raw['blocked'] else gate
            gate['summary']='BLOCK → 人工复核'
        handoff=self._tool(r,'handoff_agent','handoff.route',ex.handoff_agent,gate['blocked'] or intent=='risk');r.gates=gate['gates'];r.risk_score=1.0 if handoff['should'] else gate['risk_score'];r.handoff=handoff['should']
        for g in r.gates:r.add('harness','Compliance',g['gate'],g['note'],status='blocked' if g['status']=='BLOCK' else 'ok',gate=g,input={'raw_text':text},output=g)
        if handoff['should']:text='本轮已停止自动推介。相关问题需要人工核对适当性或风险要求；不会自动申购、代填测评或承诺收益。';rec=None;confirmed=False
        if rec and not suppressed:s.memory['last_candidates']=[x['id'] for x in rec['items']]
        if suppressed:rec=None
        if confirmed:s.memory['confirmed']=True;self.store.gmv+=10000
        s.state='SUPPRESSED' if suppressed else ('HANDOFF' if handoff['should'] else 'DELIVERED')
        outcome='suppressed' if suppressed else ('handoff' if handoff['should'] else ('converted' if confirmed else ('objection' if intent=='drawdown' else 'opened')))
        f=self.store.funnel
        if not suppressed:f['opened']+=1
        if not behaviors:f['engaged']+=1
        if rec:f['recommended']+=1
        if intent=='drawdown':f['objection']+=1
        if confirmed:f['converted']+=1
        if handoff['should']:f['handoff']+=1
        if behaviors==['assessment_complete']:f['assessed']+=1
        r.outcome=outcome;r.final_text=text;r.badcase_tags=(['信息未齐'] if match['missing'] else [])+(['风险需人工'] if handoff['should'] else [])+(['未命中知识'] if not rag['hits'] else []);r.output={'text':text,'recommendation':rec,'suitability':match,'confirmed':confirmed,'state':s.state};r.add('reflect','Reflect','归因与评测','事实按版本维护；失败样本进入人工归因',input={'badcase_tags':r.badcase_tags},output={'outcome':outcome});trace=self.store.commit(r);s.memory.setdefault('turns',[]).append({'input':message,'output':text});s.memory['turns']=s.memory['turns'][-6:]
        return {'trace_id':r.trace_id,'events':r.steps,'trace':trace,'session':{'state':s.state,'turn':s.turn},'llm':{'model':'规则话术合成（演示）','route':'deterministic','mode':'offline'},'prompt':{'used':s.prompt_version},'final_text':text,'voice':{'empathy_kind':'先解释再决策','budget':max(220,len(text)),'used':len(text)},'handoff':handoff,'gate_summary':gate['summary'],'risk_score':r.risk_score,'recommendation':rec,'suitability':match,'metrics':self.store.metrics(),'reflect':self.store.reflect(s.session_id),'sources':rag['hits']}
    def trigger(self,s,key):
        if key not in TRIGGER_MESSAGES:raise ValueError('未知行为事件')
        if key=='assessment_complete':s.memory['profile_updates']={'assessment_valid':True,'risk_level':2}
        return self.run_turn(s,TRIGGER_MESSAGES[key],[key])
    def adversarial(self,s,text):return run_gates(text,[],requires_evidence=False)
