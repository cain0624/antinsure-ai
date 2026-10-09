"""六个有限职责执行器：画像、证据、适当性、比较、话术、人工。"""
import re
from .finance import suitability,fee_quote
from . import voice
class ObserveLayer:
    def observe(self,profile,memory,message,behavior):
        profile=dict(profile)
        term=None
        if '明天' in message or '马上要用' in message:term=0
        elif '下周' in message or '一周' in message:term=0.25
        elif '半年' in message:term=6
        else:
            m=re.search(r'(\d+)\s*(天|个月|月|年)(?:后|内|就|要|用)',message)
            if m:term=int(m[1])*{'天':1/30,'个月':1,'月':1,'年':12}[m[2]]
        if term is not None:
            profile['horizon_months']=min(profile.get('horizon_months',term),term)
            memory.setdefault('profile_updates',{})['horizon_months']=profile['horizon_months']
        return {'profile':profile,'behaviors':behavior,'memory':memory,'message':message,'emotion':{'label':'担忧' if any(w in message for w in ['跌','亏','担心']) else '中性','score':.5}}
class Planner:
    def plan(self,obs,message,state,turn):
        intents=[('pause',['不打扰','停止提醒','别联系']),('risk',['保证','稳赚','借钱','贷款','信用卡','杠杆','跳过测评','代填','投诉']),('redeem',['赎回','到账']),('drawdown',['净值回落','跌','亏']),('assess',['测评','风险等级']),('confirm',['模拟申购确认']),('compare',['对比','比较','浏览']),('fees',['费用','费率','手续费']),('sip',['定投','扣款'])]
        intent=next((k for k,ws in intents if any(w in message for w in ws)),'qa')
        return {'scene':obs['behaviors'][0] if obs['behaviors'] else '主动咨询','user_type':obs['profile']['typing_hint'],'intent':intent,
                'strategy':{'pause':'停止触达','drawdown':'情绪承接与持有解释','assess':'先完成适当性','risk':'人工复核'}.get(intent,'事实解释，匹配后比较'),
                'state':state,'turn':turn,'executors':['profile_agent','rag_agent','suitability_agent','recommend_agent','script_agent','handoff_agent'],'timing':{'window':'已授权且有明确行为时回应'}}
class Executors:
    def __init__(self,products,retriever):self.products=products;self.retriever=retriever
    def profile_agent(self,obs):return obs['profile']
    def rag_agent(self,message,plan):return self.retriever.retrieve(message,plan)
    def suitability_agent(self,profile):return suitability(profile,self.products)
    def recommend_agent(self,profile,match,intent):
        ids={x['product_id'] for x in match['rows'] if x['eligible']}
        items=[]
        if intent not in ['compare','qa','fees','sip']:return None
        for p in sorted(self.products,key=lambda x:x['risk_level'],reverse=profile.get('risk_level',0)>=4):
            if p['id'] not in ids:continue
            items.append({'id':p['id'],'name':p['name'],'category':p['category'],'risk_level':p['risk_level'],
                          'fee_rate':p['fee_display'],'scope':p['scope'],'highlights':p['highlights'],'reason':'风险等级与资金使用期限均通过演示过滤','fee_quote':fee_quote(p)})
        return {'items':items[:2],'basis':'风险承受能力 → 资金期限 → 产品范围；候选只用于比较，不作投资指令','note':'全部为虚构演示基金，资料版本 2026-10-09'} if items else None
    def script_agent(self,intent,profile,rag,rec,match,version):return voice.compose(intent,profile,rag,rec,match,version)
    def handoff_agent(self,should):return {'should':should,'route':'基金服务人工复核' if should else '无需升级','sla':'演示，不承诺响应时效'}
