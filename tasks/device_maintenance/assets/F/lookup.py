def run(params):
    devices = nodes('Entity', {'class': 'device', 'name': params['serial']}, limit=5)
    ids = [row['node_id'] for row in devices]
    facts = traverse(ids, 'subject', direction='in')
    events = []
    for fact in facts:
        technician = ''
        entities = traverse([fact['node_id']], 'object_entity')
        if entities:
            technician = entities[0].get('name', '')
        date = ''
        times = traverse([fact['node_id']], 'occurrence_time')
        if times:
            date = times[0].get('start', '')
            if not date:
                date = times[0].get('raw', '')
        events.append({'node_id': fact['node_id'], 'text': fact.get('text', ''),
                       'technician': technician, 'date': date,
                       'polarity': fact.get('polarity', ''), 'modality': fact.get('modality', '')})
    return events
