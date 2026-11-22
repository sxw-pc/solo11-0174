import json
from forestgate.lightning import import_receiver, import_pulse
from forestgate.store import report


def test_first_arrival_is_only_provisional(tmp_path):
    db = tmp_path / 'forest.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    for name, x, y in [('A', 0, 0), ('B', 10, 0), ('C', 0, 10)]:
        import_receiver(db, write(name, {'kind': 'lightning_receiver', 'id': f'r-{name}',
             'receiver_id': name, 'x_km': x, 'y_km': y, 'clock_offset_us': 0}))
    for name, arrival in [('A', 100), ('B', 110), ('C', 120)]:
        import_pulse(db, write(f'p-{name}', {'kind': 'lightning_pulse', 'id': f'p-{name}',
            'burst_id': 'L1', 'receiver_id': name, 'arrival_us': arrival, 'amplitude': 3}))
    assert report(db)['lightning_locations'][0]['method'] == 'first_arrival'
    assert report(db)['lightning_locations'][0]['x_km'] == 0
