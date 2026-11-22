"""Baseline timber estimate assumes a flat ground datum of zero meters."""


def estimate(db, plot_id):
    plot = db.execute('SELECT * FROM lidar_plot WHERE id=?', (plot_id,)).fetchone()
    top = db.execute('SELECT MAX(z_m) FROM lidar_return WHERE plot_id=?', (plot_id,)).fetchone()[0]
    height = max(0, top or 0)
    volume = round(plot['area_m2'] * height * 0.02, 3)
    db.execute('INSERT INTO timber_estimate VALUES(?,?,?) ON CONFLICT(plot_id) DO UPDATE SET height_m=excluded.height_m,volume_m3=excluded.volume_m3',
               (plot_id, height, volume))
    return {'plot_id': plot_id, 'height_m': height, 'volume_m3': volume}
