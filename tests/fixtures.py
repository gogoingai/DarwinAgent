from pathlib import Path

from oak.config import RunConfig
from oak.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
from oak.kernel import TaskSpec
from oak.kernel.registration import load_assets
from oak.llm.recorded import RecordedClient

ROOT=Path(__file__).resolve().parents[1]
TASK=ROOT/'tasks/device_maintenance'


def case(name='case-a',serial='D-17',technician='林',day='2026-09-01'):
    return CaseInput(name,(CorpusBlock(SourceRef('maintenance_record',name,'row-1'),
        f'设备 {serial} 于 {day} 由{technician}维护。'),),
        (QuestionInput('q1',f'谁在什么时候维护了 {serial}？',{'serial':serial}),))


def extraction(c):
    serial=c.questions[0].parameters['serial']
    return {'facts':[{'text':f'设备 {serial} 于 2026-09-01 由林维护',
                      'subject':{'class':'device','name':serial},'predicate':'维护',
                      'object':{'entity':{'class':'person','name':'林'}},
                      'polarity':'positive','modality':'statement',
                      'time':{'raw':'2026-09-01','precision':'day','start':'2026-09-01','end':'','relative':False},
                      'evidence':[{'source_id':c.corpus[0].source.id,'quote':c.corpus[0].text}]}]}


def review(accepted=True,status='answered'):
    return {'accepted':accepted,'supported':accepted if status=='answered' else False,
            'subject_correct':True,'consistent':True,'complete':True,
            'abstention_valid':accepted if status=='abstained' else False,'feedback':'支持' if accepted else '已有设备维护记录，需重新生成'}


def client(c,answers=None,reviews=None,tools=None):
    return RecordedClient({'extraction':[extraction(c)],
                          'tools':tools or [{'action':'call','asset_id':'device_lookup','parameters':{'serial':'D-17'}},{'action':'ready'}],
                          'answer':answers or [{'status':'answered','answer':'林于2026-09-01维护。','node_ids':['n000000']}],
                          'review':reviews or [review()]})


def spec(path):
    return TaskSpec.load(TASK/'task.yaml',load_assets(TASK).export(path))


def memory_and_graph(c,s,cl=None,config=None):
    """Two-stage helpers: extract a MemoryResult, then assemble the anchored graph."""
    import asyncio
    from oak.agents import ExtractionAgent
    from oak.kg.assembler import GraphAssembler
    from oak.kernel.execution import KernelRuntime
    config=config or RunConfig()
    runtime=KernelRuntime(s.bundle,config)
    memory=asyncio.run(ExtractionAgent(runtime,cl or client(c),config,'test').extract(c.corpus))
    return runtime,memory,GraphAssembler.build(memory,s,runtime.schema)
