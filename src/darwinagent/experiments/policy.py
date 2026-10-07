"""Metric names are supplied by the independent evaluator; policy is frozen engineering code."""
from dataclasses import dataclass

# 外部故障族（操作者指令 2026-10-07「外部异常导致就不应该拦截」）：传输/限流/空补全
# 是测量层噪声而非候选质量信号——题级诊断错误串前缀分类；确定性族（ValueError/
# SandboxError 等代码与契约缺陷）照拦。
_EXTERNAL_ERROR_PREFIXES=('TransportExhausted:','EmptyCompletion:','RateLimitError:',
                          'APIConnectionError:','APITimeoutError:','InternalServerError:',
                          'APIStatusError:')


def split_faults(scores):
    """生成故障拆分为（外部传输族, 确定性族）；诊断缺失时全部按确定性处理（保守）。"""
    external=0
    for d in scores.diagnostics or ():
        if (d.get('status') or '')=='execution_error':
            err=str(d.get('error') or '')
            if err.startswith(_EXTERNAL_ERROR_PREFIXES):
                external+=1
    return external,max(0,scores.generation_faults-external)


@dataclass(frozen=True)
class AdoptionPolicy:
    primary: str
    non_decreasing: tuple[str, ...]

    def decide(self,baseline,candidate):
        failures=[]
        ext_b,det_b=split_faults(baseline)
        ext_c,det_c=split_faults(candidate)
        # 评测故障照拦（判题侧完整性）；生成故障只拦确定性族，外部族留分母并披露。
        # 未完成题数必须恰好等于外部故障数——除此之外的任何缺失（含零故障缺题）
        # 仍算 incomplete。
        if candidate.total!=baseline.total \
                or candidate.completed!=candidate.total-ext_c or det_c:
            failures.append('incomplete_evaluation')
        if candidate.evaluation_faults: failures.append('evaluation_fault')
        if det_c>det_b: failures.append('more_generation_faults')
        if set(candidate.metrics)!=set(baseline.metrics): failures.append('metric_contract_changed')
        if candidate.metrics.get(self.primary,-1)<=baseline.metrics.get(self.primary,-1): failures.append('primary_not_strictly_improved')
        for key in self.non_decreasing:
            if candidate.metrics.get(key,-1)<baseline.metrics.get(key,-1): failures.append('metric_decreased:'+key)
        return {'accepted':not failures,'reasons':failures or ['primary_strictly_improved'],
                'baseline':baseline.to_dict(),'candidate':candidate.to_dict(),
                'external_faults':{'baseline':ext_b,'candidate':ext_c}}
