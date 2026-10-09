"""基金演示数据计算。费用只由 Decimal 计算；不预测收益、不提交交易。"""
from decimal import Decimal, ROUND_HALF_UP

def fee_quote(product, amount=10000):
    a=Decimal(str(amount))
    if not a.is_finite() or a<=0 or a>Decimal('10000000'):raise ValueError('演示金额需在 0 到 1000 万元之间')
    r=Decimal(str(product['purchase_fee']))
    fee=(a-a/(1+r)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    return {'amount':str(a),'fee':str(fee),'net_amount':str(a-fee),'fee_rate':str(r),
            'note':'前端申购费演示：申购费用=金额-金额/(1+费率)；不含持有与赎回费用，不代表真实费率。'}

def suitability(profile,products):
    missing=[]
    if not profile.get('assessment_valid'):missing.append('有效风险测评')
    if not profile.get('goal'):missing.append('投资目标')
    if not profile.get('horizon_months'):missing.append('资金使用期限')
    if not profile.get('own_funds'):missing.append('自有资金确认')
    rows=[]
    for p in products:
        reasons=list(missing)
        if p['risk_level']>profile.get('risk_level',0):reasons.append('产品风险高于投资者承受等级')
        if p['min_horizon_months']>profile.get('horizon_months',0):reasons.append('投资期限不匹配')
        rows.append({'product_id':p['id'],'name':p['name'],'risk':f"R{p['risk_level']}",
                     'eligible':not reasons,'reasons':reasons})
    return {'status':'NEEDS_ASSESSMENT' if missing else 'MATCHED', 'investor_level':f"C{profile.get('risk_level',0)}",
            'missing':missing,'rows':rows,'note':'风险映射与期限过滤为演示策略，真实销售以持牌机构的适当性规则和有效测评结果为准。'}
