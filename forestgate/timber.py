"""Legacy top-return estimate (method v1), preserved as the delivered harvest basis.

It takes the highest return of a plot and assumes a flat ground datum of zero
metres.  That single value was already delivered to the harvest team, so it
is kept verbatim: later ground control points or stem surveys must not
rewrite it.  Provable ground/canopy intervals live in stand_volume.py.
"""

METHOD = 'legacy-top-return-v1'


def estimate(db, plot_id):
    plot = db.execute('SELECT * FROM lidar_plot WHERE id=?', (plot_id,)).fetchone()
    top = db.execute('SELECT MAX(z_m) FROM lidar_return WHERE plot_id=?', (plot_id,)).fetchone()[0]
    height = max(0, top or 0)
    volume = round(plot['area_m2'] * height * 0.02, 3)
    db.execute('INSERT INTO timber_estimate VALUES(?,?,?) ON CONFLICT(plot_id) DO UPDATE SET height_m=excluded.height_m,volume_m3=excluded.volume_m3',
               (plot_id, height, volume))
    return {'plot_id': plot_id, 'height_m': height, 'volume_m3': volume}
