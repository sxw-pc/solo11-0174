"""Baseline nursery advice treats raw capacitance as water percentage."""


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
