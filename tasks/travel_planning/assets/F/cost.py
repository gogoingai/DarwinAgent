def run(params):
    total = 0.0
    if params['people'] <= 0:
        return {'total': 0.0, 'valid': False}
    for choice in params['choices']:
        matches = nodes(filters={'node_id': choice['node_id']}, limit=1)
        if not matches:
            return {'total': 0.0, 'valid': False}
        row = matches[0]
        if row['entity_type'] == 'Accommodation':
            occupancy = row.get('maximum occupancy', 0)
            if occupancy <= 0 or choice.get('nights', 0) < row.get('minimum nights', 0):
                return {'total': 0.0, 'valid': False}
            total += row.get('price', 0) * ceil(params['people'] / occupancy) * choice['nights']
        elif row['entity_type'] == 'Restaurant':
            total += row.get('Average Cost', 0) * params['people']
        elif row['entity_type'] == 'Flight':
            total += row.get('Price', 0) * params['people']
        elif row['entity_type'] == 'Distance':
            distance = row.get('distance', '').split(' km')[0].replace(',', '').strip()
            if not distance.isdigit() or choice.get('mode') not in ['taxi', 'self-driving']:
                return {'total': 0.0, 'valid': False}
            km = int(distance)
            total += km * ceil(params['people'] / 4) if choice['mode'] == 'taxi' else int(km * 0.05) * ceil(params['people'] / 5)
        else:
            return {'total': 0.0, 'valid': False}
    return {'total': total, 'valid': True}
