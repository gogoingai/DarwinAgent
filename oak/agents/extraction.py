"""Atomic-fact extraction into an independent memory artifact; no graph, no questions, no gold."""
from __future__ import annotations

import asyncio
import json
from collections import namedtuple
from datetime import date

from oak.contracts import (FACT_MODALITIES, FACT_POLARITIES, FACT_PRECISIONS, FACT_VALUE_DTYPES,
                           AtomicFact, EntityRef, FactEvidence, FactTime, FactValue, MemoryResult, plain)
from .protocol import FACT_EXTRACT_PROTOCOL, ModelSession, ProtocolError

# A slice of one corpus block; bisection keeps original offsets so quotes stay locatable.
Segment = namedtuple('Segment', 'block start end')


def seg_text(seg) -> str:
    return seg.block.text[seg.start:seg.end]


def plan_batches(corpus, limit):
    """Message-boundary batches: consecutive blocks while the body stays within limit;
    one oversized message forms its own batch and keeps its original offsets."""
    batches = []; batch = []; chars = 0
    for block in corpus:
        seg = Segment(block, 0, len(block.text))
        size = len(seg_text(seg))
        if size > limit:
            if batch: batches.append(batch); batch = []; chars = 0
            batches.append([seg]); continue
        if batch and chars + size > limit:
            batches.append(batch); batch = []; chars = 0
        batch.append(seg); chars += size
    if batch: batches.append(batch)
    return batches


def bisect(segments):
    """Fixed midpoint split of a batch; single messages split on text, keeping offsets."""
    if len(segments) > 1:
        mid = len(segments) // 2
        return segments[:mid], segments[mid:]
    seg = segments[0]; mid = (seg.start + seg.end) // 2
    return (Segment(seg.block, seg.start, mid),), (Segment(seg.block, mid, seg.end),)


def _strip_fence(raw):
    t = raw.strip()
    if t.startswith('```') and t.endswith('```'):
        t = t.split('\n', 1)[1].rsplit('```', 1)[0].strip() if '\n' in t else ''
    return t


def looks_truncated(raw):
    """Explicit truncation only: an unterminated JSON string or a cut at the very end."""
    body = _strip_fence(raw) if isinstance(raw, str) else ''
    if not body:
        return False
    try:
        json.loads(body); return False
    except json.JSONDecodeError as exc:
        return 'Unterminated string' in (exc.msg or '') or (exc.pos is not None and exc.pos >= len(body) - 1)


def _canonical_value(where, obj):
    if set(obj) != {'dtype', 'value'}:
        raise ValueError(f'{where}.value: 必须是 {{dtype, value}}')
    dtype, value = obj['dtype'], obj['value']
    if dtype == 'string':
        if not isinstance(value, str) or not value.strip(): raise ValueError(f'{where}.value: 字符串值不能为空')
        return value
    if dtype == 'int':
        if type(value) is int: return str(value)
        if isinstance(value, str) and value.strip():
            digits = value.strip().replace(',', '').replace(' ', '')
            try: return str(int(digits))
            except ValueError: pass
        raise ValueError(f'{where}.value: int 值须为整数或无千分位数字（得到 {value!r}）')
    if dtype == 'float':
        if type(value) is int: return str(float(value))
        if type(value) is float: return str(value)
        if isinstance(value, str) and value.strip():
            try: return str(float(value.strip().replace(',', '').replace(' ', '')))
            except ValueError: pass
        raise ValueError(f'{where}.value: float 值须为数字或无千分位数字（得到 {value!r}）')
    if dtype == 'bool':
        if type(value) is bool: return 'true' if value else 'false'
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            return value.strip().lower()
        raise ValueError(f'{where}.value: bool 值须为 true/false（得到 {value!r}）')
    if dtype == 'date':
        if not isinstance(value, str): raise ValueError(f'{where}.value: 日期须为 ISO 字符串')
        try: date.fromisoformat(value)
        except ValueError: raise ValueError(f'{where}.value: {value!r} 不是合法 ISO 日期')
        return value
    raise ValueError(f'{where}.value.dtype: 未知类型 {dtype!r}（允许 {sorted(FACT_VALUE_DTYPES)}）')


def _make_validator(segments, classes):
    # Short payload-local source keys: the model never echoes opaque digests it can corrupt.
    allowed = {f'm{index}': seg for index, seg in enumerate(segments)}
    def validate(obj):
        if set(obj) != {'facts'} or not isinstance(obj['facts'], list) or not obj['facts']:
            raise ValueError('响应必须是 {"facts":[非空数组]}')
        facts = []
        for i, item in enumerate(obj['facts']):
            where = f'facts[{i}]'
            if not isinstance(item, dict) or set(item) != {'text', 'subject', 'predicate', 'object', 'polarity', 'modality', 'time', 'evidence'}:
                raise ValueError(f'{where}: 字段必须是 text/subject/predicate/object/polarity/modality/time/evidence')
            if not isinstance(item['text'], str) or not item['text'].strip():
                raise ValueError(f'{where}.text: 命题表述不能为空')
            if not isinstance(item['predicate'], str) or not item['predicate'].strip():
                raise ValueError(f'{where}.predicate: 谓词不能为空')
            subject = item['subject']
            if not isinstance(subject, dict) or set(subject) != {'class', 'name'} or not all(isinstance(subject[k], str) and subject[k].strip() for k in ('class', 'name')):
                raise ValueError(f'{where}.subject: 必须是 {{class, name}} 非空字符串')
            if subject['class'] not in classes:
                raise ValueError(f"{where}.subject.class: 未声明类别 {subject['class']!r}（允许：{sorted(classes)}；来源 {subject['name']!r}）")
            target = item['object']
            object_entity = object_value = None
            if target is not None:
                if not isinstance(target, dict) or set(target) > {'entity', 'value'} or not (set(target) & {'entity', 'value'}):
                    raise ValueError(f'{where}.object: 须为 null 或 {{entity:…}} 或 {{value:…}}')
                if 'entity' in target:
                    e = target['entity']
                    if not isinstance(e, dict) or set(e) != {'class', 'name'} or not all(isinstance(e[k], str) and e[k].strip() for k in ('class', 'name')):
                        raise ValueError(f'{where}.object.entity: 必须是 {{class, name}} 非空字符串')
                    if e['class'] not in classes:
                        raise ValueError(f"{where}.object.entity.class: 未声明类别 {e['class']!r}（允许：{sorted(classes)}）")
                    object_entity = EntityRef(e['class'], e['name'])
                else:
                    v = target['value']
                    if not isinstance(v, dict): raise ValueError(f'{where}.object.value: 必须是 {{dtype, value}}')
                    object_value = FactValue(v.get('dtype', ''), _canonical_value(where, v))
            if item['polarity'] not in FACT_POLARITIES:
                raise ValueError(f"{where}.polarity: 非法值 {item['polarity']!r}（允许 {sorted(FACT_POLARITIES)}）")
            if item['modality'] not in FACT_MODALITIES:
                raise ValueError(f"{where}.modality: 非法值 {item['modality']!r}（允许 {sorted(FACT_MODALITIES)}）")
            t = item['time']
            if not isinstance(t, dict) or set(t) != {'raw', 'precision', 'start', 'end', 'relative'}:
                raise ValueError(f'{where}.time: 必须是 {{raw, precision, start, end, relative}}')
            raw_time = t['raw'] if isinstance(t['raw'], str) else ''
            if not raw_time.strip():
                raw_time = '未注明'
            if t['precision'] not in FACT_PRECISIONS:
                raise ValueError(f"{where}.time.precision: 非法值 {t['precision']!r}（允许 {sorted(FACT_PRECISIONS)}）")
            if not all(isinstance(t[k], str) for k in ('start', 'end')):
                raise ValueError(f'{where}.time: start/end 须为字符串（ISO 日期或空串）')
            evidence_src = item['evidence']
            if not isinstance(evidence_src, list) or not evidence_src:
                raise ValueError(f'{where}.evidence: 每个事实至少一条逐字证据')
            evidence = []
            for j, ev in enumerate(evidence_src):
                ewhere = f'{where}.evidence[{j}]'
                if not isinstance(ev, dict) or set(ev) != {'source_id', 'quote'}:
                    raise ValueError(f'{ewhere}: 必须是 {{source_id, quote}}')
                seg = allowed.get(ev['source_id'])
                if seg is None:
                    raise ValueError(f"{ewhere}.source_id: 未知来源代号 {ev['source_id']!r}（必须逐字使用本批代号：{sorted(allowed)}）")
                quote = ev['quote']
                if not isinstance(quote, str) or not quote.strip():
                    raise ValueError(f'{ewhere}.quote: 引文不能为空')
                local = seg_text(seg).find(quote)
                if local < 0:
                    raise ValueError(f"{ewhere}.quote: 引文不是来源 {ev['source_id']} 原文的逐字子串（不得改写标点或增删字符）")
                evidence.append(FactEvidence(seg.block.source.id, quote, seg.start + local, seg.start + local + len(quote)))
            anchor = evidence[0].source_id if t['relative'] is True else ''
            try:
                fact = AtomicFact.create(text=item['text'], subject=EntityRef(subject['class'], subject['name']),
                                         predicate=item['predicate'], object_entity=object_entity,
                                         object_value=object_value, polarity=item['polarity'],
                                         modality=item['modality'],
                                         time=FactTime(raw_time, t['precision'], t['start'], t['end'], anchor),
                                         evidence=tuple(evidence))
            except ValueError as exc:
                raise ValueError(f'{where}: {exc}')
            facts.append(fact)
        return facts
    return validate


class ExtractionAgent:
    def __init__(self, runtime, client, config, namespace):
        self.runtime, self.client, self.config, self.namespace = runtime, client, config, namespace

    async def extract(self, corpus) -> MemoryResult:
        classes = frozenset(self.runtime.schema.meta.get('entity_classes') or ())
        if not classes:
            raise ProtocolError('S 未声明实体类别（meta.entity_classes）', ())
        batches = plan_batches(corpus, self.config.extraction_batch_chars)
        queue: asyncio.Queue = asyncio.Queue()
        slots = 0
        for index, batch in enumerate(batches):
            queue.put_nowait((index, tuple(batch), 0)); slots = index + 1
        sem = asyncio.Semaphore(self.config.concurrency)
        collected: dict[int, tuple | None] = {}
        raw: list[str] = []
        diagnostics: list[dict] = []
        failures: list[str] = []

        async def worker():
            while True:
                try: slot, segments, depth = queue.get_nowait()
                except asyncio.QueueEmpty: return
                async with sem:
                    session = ModelSession(self.client, self.config, f'{self.namespace}_extract_{slot}')
                    payload = {'schema': {'entity_classes': sorted(classes)},
                               'sources': [{'source_id': f'm{index}', 'text': seg_text(s),
                                            'metadata': plain(s.block.metadata)}
                                           for index, s in enumerate(segments)]}
                    try:
                        facts = await session.request(self.config.extraction_role,
                            FACT_EXTRACT_PROTOCOL + '\n任务抽取指引：\n' + self.runtime.prompt('extract'),
                            payload, _make_validator(segments, classes),
                            max_tokens=self.config.extraction_max_tokens)
                        collected[slot] = (facts, session.raw, session.events)
                        raw.extend(session.raw)
                        diagnostics.append({'batch': slot, 'depth': depth, 'status': 'ok', 'facts': len(facts),
                                            'chars': sum(len(seg_text(s)) for s in segments), 'events': session.events})
                    except ProtocolError as exc:
                        raws = getattr(exc, 'raw_outputs', ())
                        raw.extend(raws)
                        last = raws[-1] if raws else ''
                        splittable = len(segments) > 1 or len(seg_text(segments[0])) > 256
                        parse_failure = 'JSONDecodeError' in str(exc) or 'Unterminated' in str(exc)
                        if depth < self.config.extraction_bisect_depth and splittable and parse_failure and looks_truncated(last):
                            left, right = bisect(segments)
                            diagnostics.append({'batch': slot, 'depth': depth, 'status': 'bisected',
                                                'error': str(exc)[:500]})
                            nonlocal slots
                            for part in (left, right):
                                if part: queue.put_nowait((slots, tuple(part), depth + 1)); slots += 1
                        else:
                            collected[slot] = None
                            failures.append(f'batch {slot}: {exc}')
                            diagnostics.append({'batch': slot, 'depth': depth, 'status': 'failed', 'error': str(exc)[:500]})

        await asyncio.gather(*(worker() for _ in range(self.config.concurrency)))
        if failures:
            raise ProtocolError('Extraction failed: ' + '; '.join(failures), raw)
        facts: list[AtomicFact] = []
        seen: dict[str, str] = {}
        duplicates = 0
        for slot in sorted(collected):
            outcome = collected[slot]
            if outcome is None: continue
            for fact in outcome[0]:
                if fact.id in seen:
                    duplicates += 1; continue
                seen[fact.id] = slot; facts.append(fact)
        if duplicates:
            diagnostics.append({'status': 'note', 'duplicate_facts_deduplicated': duplicates})
        return MemoryResult({b.source.id: b for b in corpus}, tuple(facts), tuple(raw), tuple(diagnostics))
