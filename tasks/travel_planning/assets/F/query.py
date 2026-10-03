def run(params):
    return nodes(params['type'], params.get('filters', {}), params.get('limit', 40))
