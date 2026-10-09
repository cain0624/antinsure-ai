"""运行事实观测；本地演示指标与业务实验指标严格区分。"""
import time,uuid
from collections import Counter
from dataclasses import dataclass,field,asdict
@dataclass
class TraceRecord:
    trace_id:str
    session_id:str
    user_id:str
    created_at:float
    steps:list=field(default_factory=list)
    prompt_version:str='v3.0'
    model_route:str='tool'
    model_name:str='规则话术合成（演示）'
    gates:list=field(default_factory=list)
    risk_score:float=0
    outcome:str='opened'
    handoff:bool=False
    final_text:str=''
    badcase_tags:list=field(default_factory=list)
    input:dict=field(default_factory=dict)
    output:dict=field(default_factory=dict)
    def add(self,layer,agent,action,detail='',status='ok',latency_ms=0,**meta):
        s={'seq':len(self.steps)+1,'layer':layer,'agent':agent,'action':action,'detail':detail,'status':status,'latency_ms':latency_ms,'meta':meta};self.steps.append(s);return s
    def to_dict(self,with_steps=True):
        d=asdict(self);d.update({'created_at_str':time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(self.created_at)), 'tokens_in':0,'tokens_out':0,'cost_yuan':0,'total_latency_ms':round(sum(x['latency_ms'] for x in self.steps),2),'step_count':len(self.steps),'mode':'offline-demo'})
        if not with_steps:d.pop('steps')
        return d
class TraceStore:
    def __init__(self):
        self.records={};self.order=[];self.alerts=[];self.regression_runs=[]
        self.funnel=dict.fromkeys(['opened','engaged','objection','recommended','assessed','converted','handoff'],0)
        self.gate_stats=dict.fromkeys(['G1','G2','G3','G4'],0);self.gate_checks=dict(self.gate_stats);self.gmv=0;self.badcase_counter=Counter();self.regression_runner=None
    def new_trace(self,sid,uid):return TraceRecord('tr_'+uuid.uuid4().hex[:12],sid,uid,time.time())
    def commit(self,r):
        self.records[r.trace_id]=r;self.order.append(r.trace_id)
        for g in r.gates:
            self.gate_checks[g['gate']]+=1
            if g['status']!='PASS':self.gate_stats[g['gate']]+=1
        self.badcase_counter.update(r.badcase_tags)
        if r.handoff:self.alerts.append({'ts_str':time.strftime('%H:%M:%S'),'level':'风险边界','title':'已停止自动推介','detail':'需人工复核 '+r.trace_id,'severity':'warn'})
        return r.to_dict()
    def replay(self,tid):
        r=self.records.get(tid)
        if not r:return None
        return {'note':'历史快照回看：展示当时输入、工具输出和闸门结果，不代表重新调用模型。','prompt_version':r.prompt_version,'replayed_at':time.strftime('%H:%M:%S'),'diff_hint':'未执行新推理，未生成新交易；可依据原始输入复现问题。','steps':r.steps,'input':r.input,'output':r.output}
    def metrics(self):
        n=len(self.order);sessions=len({r.session_id for r in self.records.values()});checks=sum(self.gate_checks.values());matched=sum(bool(r.output.get('recommendation')) for r in self.records.values());blocked=sum(r.handoff for r in self.records.values())
        return {'funnel':dict(self.funnel),'touch':{'当前演示会话数':sessions,'已停止主动触达':sum(r.outcome=='suppressed' for r in self.records.values())},
                'dialogue':{'当前演示轮次':n,'平均轮次':round(n/max(1,sessions),1)},
                'conversion':{'模拟申购确认数':self.funnel['converted'],'模拟申购GMV（元）':self.gmv,'真实交易': '未接入'},
                'quality':{'有可比较候选的轮次':matched,'转人工轮次':blocked,'闸门拦截比例':round(sum(self.gate_stats.values())/max(1,checks),3)},
                'efficiency':{'有Trace的轮次':n,'真实模型调用':'未接入','推理成本':'未发生','数据来源':'当前浏览器演示运行'},
                'models':{},'gate_stats':dict(self.gate_stats),'gate_checks':dict(self.gate_checks),'badcase_counter':dict(self.badcase_counter),'alerts':self.alerts[-12:][::-1],'trace_count':n}
    def reflect(self,sid):
        rs=[r for r in self.records.values() if r.session_id==sid];tags=Counter(t for r in rs for t in r.badcase_tags)
        return {'turns':len(rs),'badcase_clusters':[{'cluster':k,'count':v,'suggestion':'核对该轮输入、知识版本、适当性结果与闸门命中，再加入离线回归。'} for k,v in tags.items()], 'feed_back_to':['人工归因队列','版本化知识库','Prompt回归集'], 'eval':{'matched_turns':sum(bool(r.output.get('recommendation')) for r in rs),'confirmed':sum(r.outcome=='converted' for r in rs),'drop_point':'待完成测评' if any('信息未齐' in r.badcase_tags for r in rs) else '需业务埋点验证'}}
    def regression(self):
        result=self.regression_runner();self.regression_runs.append(result);return result
