def run(params):
    table = params.get('table')
    city = params.get('city')
    limit = params.get('limit', 40)
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if table == 'attractions':
        rows = nodes(entity_type='attraction', filters={'city': city}, limit=5000)
        fields = ['name', 'city', 'address', 'phone', 'website', 'latitude', 'longitude']
    elif table == 'restaurants':
        rows = nodes(entity_type='restaurant', filters={'city': city}, limit=5000)
        fields = ['name', 'city', 'cuisines', 'average_cost', 'aggregate_rating']
    elif table == 'accommodations':
        rows = nodes(entity_type='accommodation', filters={'city': city}, limit=5000)
        fields = ['name', 'city', 'room_type', 'price', 'house_rules', 'minimum_nights', 'maximum_occupancy', 'review_rate_number']
    elif table == 'flights':
        rows = nodes(entity_type='flight', filters={'dest_city': city}, limit=5000)
        if len(rows) == 0:
            rows = nodes(entity_type='flight', filters={'origin_city': city}, limit=5000)
        fields = ['flight_number', 'price', 'dep_time', 'arr_time', 'elapsed_time', 'flight_date', 'origin_city', 'dest_city', 'distance']
    elif table == 'cities':
        rows = nodes(entity_type='city', filters={'city': city}, limit=10)
        fields = ['city', 'state']
    else:
        rows = ()
        fields = []
    out = []
    total = len(rows)
    for r in rows[:limit]:
        item = {'node_id': r.get('node_id'), 'entity_type': r.get('entity_type'), 'source_ids': list(r.get('source_ids', ()))}
        for f in fields:
            item[f] = r.get(f)
        out.append(item)
    note = ''
    if total > limit:
        note = 'truncated: matched ' + str(total) + ' rows, returned ' + str(limit)
    return {'rows': out, 'scanned': total, 'note': note}