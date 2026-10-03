def check(candidate):
    if candidate['status'] == 'abstained':
        return {'ok': True, 'issues': []}
    serial = candidate['parameters']['serial']
    valid = False
    for row in candidate['evidence']:
        if row.get('entity_type') == 'AtomicFact' and serial in row.get('text', ''):
            valid = True
    return {'ok': valid, 'issues': [] if valid else ['Evidence does not belong to the requested device']}
