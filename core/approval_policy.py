"""Narrow exemptions for routine new configuration, never production results."""


def routine_quality_creation(snapshot):
    entities = {'sample_data_point': 'SampleDataPoints', 'sample_test': 'SampleTests',
                'sampling_plan': 'SamplingPlans'}
    entity = entities.get(snapshot.get('definition_kind'))
    body = snapshot.get('body')
    return bool(
        entity and snapshot.get('kind') == 'inspection_definition'
        and snapshot.get('action') == 'create' and snapshot.get('api') == 'modeling'
        and snapshot.get('entity') == entity and snapshot.get('method') == 'POST'
        and snapshot.get('path') == '/api/' + entity
        and snapshot.get('before') is None and not snapshot.get('target_id')
        and not snapshot.get('quality_signature') and isinstance(body, dict)
        and body.get('name') == snapshot.get('name') and bool(body.get('name'))
        and body.get('revision') == snapshot.get('revision') and bool(body.get('revision'))
    )
