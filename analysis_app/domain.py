import math


def describe(records):
    return {'hours_total': math.fsum(row['hours'] for row in records), 'crew_total': sum(row['crew'] for row in records), 'closed_records': sum(row['closed'] for row in records)}
