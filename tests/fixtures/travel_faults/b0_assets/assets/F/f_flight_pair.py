def run(params):
    org = params.get('org')
    dest = params.get('dest')
    date_from = params.get('date_from')
    date_to = params.get('date_to')
    outbound = nodes(entity_type='flight', filters={'origin_city': org, 'dest_city': dest}, limit=5000)
    inbound = nodes(entity_type='flight', filters={'origin_city': dest, 'dest_city': org}, limit=5000)
    ob = None
    for r in outbound:
        if date_from is not None and date_from != '' and r.get('flight_date') != date_from:
            continue
        if ob is None or (r.get('price') or 0.0) < (ob.get('price') or 0.0):
            ob = r
    ib = None
    for r in inbound:
        if date_to is not None and date_to != '' and r.get('flight_date') != date_to:
            continue
        if ib is None or (r.get('price') or 0.0) < (ib.get('price') or 0.0):
            ib = r
    out = {'outbound': None, 'inbound': None, 'outbound_candidates_scanned': len(outbound), 'inbound_candidates_scanned': len(inbound)}
    if ob is not None:
        out['outbound'] = {'node_id': ob.get('node_id'), 'entity_type': 'flight', 'source_ids': list(ob.get('source_ids', ())), 'flight_number': ob.get('flight_number'), 'price': ob.get('price'), 'dep_time': ob.get('dep_time'), 'arr_time': ob.get('arr_time'), 'flight_date': ob.get('flight_date'), 'origin_city': ob.get('origin_city'), 'dest_city': ob.get('dest_city')}
    if ib is not None:
        out['inbound'] = {'node_id': ib.get('node_id'), 'entity_type': 'flight', 'source_ids': list(ib.get('source_ids', ())), 'flight_number': ib.get('flight_number'), 'price': ib.get('price'), 'dep_time': ib.get('dep_time'), 'arr_time': ib.get('arr_time'), 'flight_date': ib.get('flight_date'), 'origin_city': ib.get('origin_city'), 'dest_city': ib.get('dest_city')}
    return out