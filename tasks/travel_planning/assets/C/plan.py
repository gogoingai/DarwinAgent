def check(candidate):
    if candidate['status'] == 'abstained':
        return {'ok': True, 'issues': []}
    plan = candidate['structured_answer']
    request = candidate['parameters']
    evidence = candidate['evidence']
    issues = []
    if len(plan) != request['days']:
        issues.append('Plan length differs from requested days')
    if request['people_number'] <= 0:
        issues.append('People must be positive')
        return {'ok': False, 'issues': issues}
    seen_meals = []
    nights = []
    visited = []
    modes = []
    total = 0.0
    previous = request['org']
    for i, day in enumerate(plan):
        if day['days'] != i + 1:
            issues.append('Day sequence is not continuous')
        city_text = day['current_city']
        moving = city_text.startswith('from ') and ' to ' in city_text
        origin = city_text[5:].split(' to ')[0].strip() if moving else city_text.strip()
        destination = city_text[5:].split(' to ')[1].strip() if moving else city_text.strip()
        if origin != previous:
            issues.append('City continuity violated')
        previous = destination
        if destination != request['org'] and destination not in visited:
            visited.append(destination)
        transport = day['transportation']
        if moving:
            if 'Flight Number:' in transport:
                number = transport.split('Flight Number:')[1].split(',')[0].strip()
                matched = [r for r in evidence if r['entity_type'] == 'Flight' and r.get('Flight Number') == number and r.get('OriginCityName') == origin and r.get('DestCityName') == destination]
                if not matched:
                    issues.append('Flight is not supported for requested direction')
                else:
                    flight = matched[0]
                    total += flight.get('Price', 0) * request['people_number']
                    if i < len(request['date']) and flight.get('FlightDate') != request['date'][i]:
                        issues.append('Flight date differs from request')
                    if flight.get('DepTime', '') not in transport or flight.get('ArrTime', '') not in transport:
                        issues.append('Flight times differ from cited row')
                modes.append('flight')
            elif 'Self-driving' in transport or 'Taxi' in transport:
                matched = [r for r in evidence if r['entity_type'] == 'Distance' and r.get('origin') == origin and r.get('destination') == destination]
                if not matched:
                    issues.append('Ground route is not supported')
                else:
                    distance_text = matched[0].get('distance', '').split(' km')[0].replace(',', '').strip()
                    if not distance_text.isdigit():
                        issues.append('Ground distance is not calculable')
                    else:
                        km = int(distance_text)
                        if 'Taxi' in transport:
                            total += km * ceil(request['people_number'] / 4)
                        else:
                            total += int(km * 0.05) * ceil(request['people_number'] / 5)
                modes.append('ground')
            else:
                issues.append('Moving day lacks a grounded transport mode')
            if 'from ' + origin + ' to ' + destination not in transport:
                issues.append('Transport text does not match city movement')
        elif transport != '-':
            issues.append('Stay day has unexpected intercity transport')
        constraint = request['local_constraint'].get('transportation')
        if constraint and ((constraint == 'no flight' and 'Flight Number:' in transport) or (constraint == 'no self-driving' and 'Self-driving' in transport)):
            issues.append('Transportation constraint violated')
        for field in ['breakfast', 'lunch', 'dinner', 'accommodation', 'attraction']:
            value = day[field]
            if value == '-':
                if field == 'accommodation':
                    nights.append('')
                if field == 'attraction' or moving and (field != 'accommodation' or i + 1 == request['days']):
                    continue
                issues.append('Missing required daily choice: ' + field)
                continue
            names = [v.strip() for v in value.split(';') if v.strip()] if field == 'attraction' else [value]
            expected_type = 'Restaurant' if field in ['breakfast', 'lunch', 'dinner'] else 'Accommodation' if field == 'accommodation' else 'Attraction'
            for label in names:
                parts = label.rsplit(',', 1)
                if len(parts) != 2:
                    issues.append('Choice requires an explicit city')
                    continue
                name, city = parts[0].strip(), parts[1].strip()
                matched = [r for r in evidence if r['entity_type'] == expected_type and name == r.get('Name', r.get('NAME', '')) and city == r.get('City', r.get('city', ''))]
                if not matched:
                    issues.append('Choice is not grounded in cited table rows: ' + field)
                    continue
                row = matched[0]
                if city != destination and not (moving and field in ['breakfast', 'lunch', 'dinner'] and city == origin):
                    issues.append('Choice belongs to a different city')
                if field in ['breakfast', 'lunch', 'dinner']:
                    if row['node_id'] in seen_meals:
                        issues.append('Restaurant reused')
                    seen_meals.append(row['node_id'])
                    total += row.get('Average Cost', 0) * request['people_number']
                    cuisine = request['local_constraint'].get('cuisine', [])
                    if cuisine and not any(c in row.get('Cuisines', '') for c in cuisine):
                        issues.append('Cuisine constraint violated')
                elif field == 'accommodation':
                    occupancy = row.get('maximum occupancy', 0)
                    if occupancy <= 0:
                        issues.append('Invalid accommodation occupancy')
                    else:
                        total += row.get('price', 0) * ceil(request['people_number'] / occupancy)
                    nights.append(row['node_id'])
                    requested_type = request['local_constraint'].get('room type')
                    if requested_type and requested_type != row.get('room type'):
                        issues.append('Room type constraint violated')
                    rule = request['local_constraint'].get('house rule')
                    if rule and rule in row.get('house_rules', ''):
                        issues.append('House rule constraint violated')
    if previous != request['org']:
        issues.append('Plan does not return to origin')
    allowed_destinations = [r.get('city') for r in evidence if r['entity_type'] == 'City' and r.get('state') == request['dest']]
    if request['dest'] not in visited and not any(city in visited for city in allowed_destinations):
        issues.append('Requested destination was not visited')
    if len(visited) != request['visiting_city_number']:
        issues.append('Distinct visiting city count differs from request')
    if 'flight' in modes and 'ground' in modes:
        issues.append('Mixed intercity transport modes')
    for row in evidence:
        if row['entity_type'] == 'Accommodation' and row['node_id'] in nights:
            longest = 0
            current = 0
            for value in nights:
                current = current + 1 if value == row['node_id'] else 0
                longest = max(longest, current)
            if longest < row.get('minimum nights', 0):
                issues.append('Minimum consecutive stay constraint violated')
    if total > request['budget']:
        issues.append('Budget exceeded')
    return {'ok': not issues, 'issues': issues}
