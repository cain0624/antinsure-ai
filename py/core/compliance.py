"""基金四道闸，针对原始文本检查；拒绝无依据事实、承诺收益与绕过测评。"""
import re,time
RISK_NOTE='基金有风险，投资需谨慎；过往业绩不代表未来表现。'
def run_gates(text,evidence=None,matched=True,requires_evidence=True):
    start=time.perf_counter();evidence=evidence or [];gates=[]
    def add(g,name,status,hits,note):gates.append({'gate':g,'name':name,'status':status,'hits':[{'reason':h} for h in hits],'note':note,'latency_ms':round((time.perf_counter()-start)*1000,2)})
    # 先剔除否定式风险声明再检测承诺，避免“不能保证收益”误杀。
    clean=re.sub(r'(?:不能|无法|不应|不|禁止|不得)(?:承诺|保证|确保)?(?:保本|保收益|收益|稳赚|不会亏损|零风险|稳赚不赔)','',text)
    bad_numbers=[]
    source=' '.join(evidence)
    for n in re.findall(r'\d+(?:\.\d+)?\s*(?:%|元|万元|天|个月)',text):
        if n.replace(' ','') not in source.replace(' ',''):bad_numbers.append('无依据数字：'+n)
    add('G1','数字与费用依据','BLOCK' if bad_numbers else 'PASS',bad_numbers,'金额、费率和期限必须来自可核验资料或计算工具')
    no_fact=requires_evidence and not evidence
    add('G2','证据与版本','BLOCK' if no_fact else 'PASS',['未命中有效资料'] if no_fact else [],'无有效依据时仅澄清或转人工，不生成产品事实')
    absolute=[w for w in ['全市场最好','最赚钱','百分百','绝对安全','最佳基金'] if w in clean]
    add('G3','宣传用语','BLOCK' if absolute else 'PASS',absolute,'不使用绝对化宣传或无依据排名')
    risk=[]
    patterns=[r'稳赚(?:不赔|不亏)?',r'保证.{0,5}(?:收益|盈利|赚钱)',r'保本(?:保息|保收益)?',r'不会亏(?:损|钱)?',r'零风险',r'年化\d+(?:\.\d+)?%.*(?:保证|锁定)',r'(?:不用|无需|跳过|绕过).{0,4}(?:风险测评|测评|适当性)',r'(?:测评|答案).{0,5}(?:代填|改高|调高)',r'(?:贷款|借钱|信用卡|杠杆).{0,8}(?:买|投资|申购|抄底)']
    for pat in patterns:
        if re.search(pat,clean):risk.append('承诺收益、绕过测评或借贷投资')
    if not matched:risk.append('适当性不匹配')
    add('G4','适当性与风险边界','BLOCK' if risk else 'PASS',risk,'投资者风险、资金来源与产品范围必须匹配')
    blocked=any(g['status']=='BLOCK' for g in gates)
    return {'gates':gates,'blocked':blocked,'sanitized':False,'summary':'BLOCK → 人工复核' if blocked else 'PASS',
            'risk_score':1.0 if blocked else 0.0,'final_text':'' if blocked else text}
