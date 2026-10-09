"""可审计的词项检索基线，按生效版本过滤；不冒充向量检索或蕴含模型。"""
import re
from datetime import date
class Retriever:
    def __init__(self,corpus):self.corpus=corpus
    def retrieve(self,question,planner_ctx=None):
        now=date.today().isoformat();words=set(re.findall(r'[\u4e00-\u9fff]{2}|[a-zA-Z]+',question))
        valid=[d for d in self.corpus if d.get('approved') and d['effective_from']<=now and (not d.get('effective_to') or now<d['effective_to'])]
        scored=[]
        for d in valid:
            score=sum(3 for k in d['keywords'] if k in question)+len(words.intersection(set(re.findall(r'[\u4e00-\u9fff]{2}',d['text']))))
            if score:scored.append((score,d))
        scored.sort(key=lambda x:x[0],reverse=True)
        return {'hits':[dict(d,score=n) for n,d in scored[:3]],'recall_pool':len(scored),'method':'版本过滤 + 词项召回','query':question}
