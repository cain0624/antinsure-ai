"""演示不调用真实模型，不伪造调用比例、token 或推理成本。"""
ROUTES={k:{'name':v,'mode':'offline'} for k,v in [('primary','规则话术合成'),('tool','确定性工具'),('sensitive','人工复核边界')]}
