import json
from forestgate.wildlife import import_camera, import_frame
from forestgate.store import report


def test_track_hint_count_is_only_local(tmp_path):
    db = tmp_path / 'forest.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    for camera, x in [('C1', 0), ('C2', 2)]:
        import_camera(db, write(camera, {'kind': 'wildlife_camera', 'id': f'camera-{camera}',
            'camera_id': camera, 'block_id': 'B1', 'x_km': x, 'y_km': 0}))
        import_frame(db, write(f'frame-{camera}', {'kind': 'wildlife_frame', 'id': f'frame-{camera}',
            'camera_id': camera, 'species': 'deer', 'track_hint': 'T1', 'at': '2026-11-22T08:00:00Z',
            'quality': 0.9}))
    assert report(db)['wildlife_counts'][0]['count'] == 2
