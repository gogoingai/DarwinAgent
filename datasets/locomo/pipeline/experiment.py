"""Frozen evaluation source formatting only; no generation or optimization loop."""
def transcript(conv, audit_metadata=False):
    lines=[]
    for s in conv.sessions:
        lines.append(f'会话{s.no} 日期{s.date_raw}')
        for t in s.turns:
            lines.append(f'[{t.dia_id}] {t.speaker}: {t.text}')
            if getattr(t,'image_caption',''):
                lines.append(f'[{t.dia_id} 原始机器图片说明，非说话人台词] {t.image_caption}')
            if audit_metadata and getattr(t,'image_query',''):
                lines.append(f'[{t.dia_id} 搜图词，仅意图不是事实] {t.image_query}')
    lines.append('【数据集派生标注，不能覆盖明确原文】')
    lines.extend(f'[{dia}] {subject}: {body}' for dia,subject,body in getattr(conv,'observations',[]))
    lines.extend(f'[事件标注 {day}] {subject}: {body}' for day,subject,body in getattr(conv,'events',[]))
    return '\n'.join(lines)


def context_for(conv, idx):
    # The evaluator must see all sessions: absence in QA evidence sessions is not absence in the corpus.
    return transcript(conv)
