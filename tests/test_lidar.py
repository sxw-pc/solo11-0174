import json
from forestgate.lidar import import_plot, import_return
from forestgate.store import report


def test_top_return_uses_provisional_zero_ground(tmp_path):
    db = tmp_path / 'forest.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    import_plot(db, write('plot', {'kind': 'lidar_plot', 'id': 'P1', 'plot_id': 'P1',
        'block_id': 'B1', 'area_m2': 100}))
    value = import_return(db, write('return', {'kind': 'lidar_return', 'id': 'R1',
        'plot_id': 'P1', 'beam_id': 'L1', 'z_m': 30, 'return_no': 1}))
    assert value['volume_m3'] == 60
    assert report(db)['timber_estimates'][0]['height_m'] == 30
