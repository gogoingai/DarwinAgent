def check(candidate):
    if candidate['status'] == 'abstained':
        return {'ok': True, 'issues': []}
    valid = any(row.get('serial') == candidate['parameters']['serial'] for row in candidate['evidence'])
    return {'ok': valid, 'issues': [] if valid else ['Evidence belongs to another device']}
