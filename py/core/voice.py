"""简短、有依据的基金沟通；共情不构成买入诱导。"""
def compose(intent,profile,rag,rec,match,version):
    if intent=='pause':return '收到，我会停止主动提醒。您需要时再来咨询。'
    if intent=='risk':return '这类要求涉及收益承诺或风险边界，我不能据此推介产品。可以由人工解释风险与适当性要求。'
    if intent=='redeem':return '理解您想取回资金。赎回费用、到账安排与持有时长有关，请先核对产品资料；不能把短期波动直接当作买卖指令。'
    if match['missing']:return '先不急着选基金。请先完成有效风险测评，再明确投资目标、资金使用期限和自有资金情况；当前只提供基础知识解释。'
    if intent=='drawdown':return '看到净值回落会担心很正常。先核对资金使用时间与能承受的波动，再查看产品风险；我不会给出抄底或立即卖出的指令。'
    if intent=='confirm':return '模拟申购已确认。这只是演示记录，不发生真实扣款或交易；持有期间可查看费用、风险与赎回规则。'
    if not rag['hits']:return '这个问题尚未检索到当前有效的资料，我不会猜测产品事实。请补充具体产品或转人工核对。'
    if version=='v1.0':return '您好，可以先说明您希望了解的基金问题。基金有风险，投资需谨慎。'
    if rec and rec['items']:
        names='、'.join(x['name'] for x in rec['items'])
        return f'先看您的目标与用款时间。通过演示风险与期限过滤的比较候选是：{names}。可以先了解投资范围、费用和赎回规则，自主决定是否继续。基金有风险，投资需谨慎。'
    return '先把规则弄清楚再决定。'+rag['hits'][0]['text']
