def check(candidate):
    issues = []
    ans = None
    if candidate.get('structured_answer') is not None:
        ans = candidate.get('structured_answer')
    elif candidate.get('answer') is not None:
        ans = candidate.get('answer')
    status = candidate.get('status')
    if status is None:
        issues.append('missing status')
    if ans is None:
        if status == 'answered':
            issues.append('answered status without an answer payload')
        if not issues:
            return {'ok': True, 'issues': []}
    if not isinstance(ans, list):
        issues.append('answer is not an array')
        return {'ok': False, 'issues': issues}
    required = ['days', 'current_city', 'transportation', 'breakfast', 'lunch', 'dinner', 'attraction', 'accommodation']
    for i, day in enumerate(ans):
        if not isinstance(day, dict):
            issues.append('day ' + str(i) + ' is not an object')
            continue
        for k in required:
            if k not in day:
                issues.append('day ' + str(i) + ' missing field ' + k)
        d = day.get('days')
        if isinstance(d, bool) or not isinstance(d, int):
            issues.append('day ' + str(i) + ' field days is not an integer')
        for k in ['current_city', 'transportation', 'breakfast', 'lunch', 'dinner', 'attraction', 'accommodation']:
            v = day.get(k)
            if v is not None and not isinstance(v, str):
                issues.append('day ' + str(i) + ' field ' + k + ' is not a string')
    return {'ok': len(issues) == 0, 'issues': issues}