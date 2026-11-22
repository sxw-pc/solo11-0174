"""Legacy capacitance-only nursery advice (original ingestion projection).

This projection deliberately treats raw capacitance as if it were the water
percentage and compares it directly with 30.  During saline seasons C = W + S,
so the number is also driven by salt and this advice is provisional.  It is
kept unchanged so the original single-frequency intake stays reproducible; the
feasible-range and valve-planning logic lives in ``moisture_bounds`` and is
stored in separate versioned tables that never overwrite this projection.
"""


def advise(db, plot_id):
    plot = db.execute('SELECT * FROM nursery_plot WHERE id=?', (plot_id,)).fetchone()
    reading = db.execute('SELECT * FROM soil_reading WHERE plot_id=? ORDER BY at DESC LIMIT 1',
                         (plot_id,)).fetchone()
    if reading is None:
        return {'plot_id': plot_id, 'status': 'unmeasured'}
    dry = reading['capacitance'] < 30
    liters = plot['area_m2'] * 4 if dry else 0
    reason = 'capacitance_low' if dry else 'capacitance_ok'
    db.execute('INSERT INTO irrigation_advice VALUES(?,?,?) ON CONFLICT(plot_id) DO UPDATE SET liters=excluded.liters,reason=excluded.reason',
               (plot_id, liters, reason))
    return {'plot_id': plot_id, 'liters': liters, 'reason': reason}
