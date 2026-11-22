import json
from forestgate.soil import import_plot, import_reading
from forestgate.store import report


def test_capacitance_advice_is_provisional(tmp_path):
    db = tmp_path / 'forest.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    import_plot(db, write('plot', {'kind': 'nursery_plot', 'id': 'P1', 'plot_id': 'P1',
        'block_id': 'B1', 'area_m2': 100}))
    advice = import_reading(db, write('reading', {'kind': 'soil_reading', 'id': 'S1',
        'plot_id': 'P1', 'at': '2026-11-22T08:00:00Z', 'capacitance': 50}))
    assert advice['liters'] == 0
    assert report(db)['irrigation_advice'][0]['reason'] == 'capacitance_ok'
