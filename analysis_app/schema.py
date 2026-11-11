FIELDS = {'observation_id': 'str', 'depot': 'str', 'region': 'str', 'week': 'int', 'hours': 'float', 'crew': 'int', 'closed': 'int', 'structure_version': 'str'}
REQUIRED = ['observation_id', 'depot', 'region', 'week', 'hours', 'crew', 'closed', 'structure_version']
ENTITY = 'depot'
GROUP = 'region'
MEASURE = 'hours'
PRODUCT = 'hierarchy-reconciliation'
UNIT = '工时'
