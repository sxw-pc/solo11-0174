"""Feasible-interval spectral unmixing for mixed forest-floor pixels.

The legacy :mod:`forestgate.scan` pipeline labels each pixel with its single
nearest reference material.  A real pixel mixes pine litter, grass, wet soil
and shade, and several mixtures can explain the same three-band observation.
This module therefore never returns a point estimate.  It builds one joint
linear program over every plot of a transect and reports *provable* lower and
upper bounds on each material's fraction and dry mass, on the unexplained
residual and on the compartment fuel load.

Three structural facts are enforced instead of hidden:

* **Non-identifiability.**  When a reference spectrum lies inside the convex
  hull of the others (e.g. wet leaf exactly halfway between pine and grass),
  two compositions share residual zero.  Per-pixel NNLS would pick one and
  collapse a genuine interval to a fake point; interval optimization keeps
  the full feasible range and names the non-identifiable material pairs.
* **Shared calibration.**  All pixels of a transect share one instrument
  calibration.  A single calibration-bias variable is constrained once,
  jointly over every plot, so pixels cannot each choose a favourable
  correction.  The reported bias interval is likewise joint.
* **Coverage.**  Ground clipping only proves dry mass inside the clipped
  cells.  Confirmed mass, continuously-covered area and pending cells are
  reported separately; clipped mass is never extrapolated to the transect.

All arithmetic is exact rational (:mod:`forestgate.lp`); no third-party
solver is required.
"""
from fractions import Fraction

from .lp import linprog


def F(value):
    """Parse any JSON numeric input into an exact :class:`Fraction`."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ValueError('numeric_required')
    if isinstance(value, (int, float)):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    raise ValueError('numeric_required')


# ---------------------------------------------------------------------------
# model construction
# ---------------------------------------------------------------------------

class _Model:
    """Accumulate variables and linear constraints for one analysis stage.

    Constraints are kept sparse as (index, coefficient) pairs so variables
    added later (aggregates, clipping totals) never invalidate earlier rows.
    """

    def __init__(self):
        self.var_name = []
        self.bounds = []
        self.ub = []  # (sparse row, rhs)
        self.eq = []

    def var(self, name, low=0, high=None):
        idx = len(self.var_name)
        self.var_name.append(name)
        self.bounds.append((low, high))
        return idx

    def constrain(self, coefficients, sense, rhs):
        sparse = [(idx, coef) for idx, coef in coefficients if coef != 0]
        if sense == '=':
            self.eq.append((sparse, rhs))
        elif sense == '<=':
            self.ub.append((sparse, rhs))
        else:  # '>=' flipped into '<=' with negated coefficients
            self.ub.append(([(idx, -coef) for idx, coef in sparse], -rhs))

    def _dense(self, rows):
        n = len(self.var_name)
        dense = []
        for sparse, _rhs in rows:
            row = [Fraction(0)] * n
            for idx, coef in sparse:
                row[idx] += coef
            dense.append(row)
        return dense

    def solve(self, objective):
        n = len(self.var_name)
        if len(objective) < n:
            objective = objective + [Fraction(0)] * (n - len(objective))
        return linprog(objective,
                       self._dense(self.ub), [rhs for _, rhs in self.ub],
                       self._dense(self.eq) or None,
                       [rhs for _, rhs in self.eq] or None,
                       self.bounds)

    def extrema(self, idx):
        """Provable (min, max) of one variable over the feasible region."""
        zeros = [Fraction(0)] * len(self.var_name)
        c_min = zeros[:]; c_min[idx] = Fraction(1)
        c_max = zeros[:]; c_max[idx] = Fraction(-1)
        lo = self.solve(c_min)
        hi = self.solve(c_max)
        if lo['status'] != 'optimal' or hi['status'] != 'optimal':
            raise ValueError('model_infeasible')
        return lo['fun'], -hi['fun']


def _validate_mixture(doc):
    required = ('kind', 'id', 'block_id', 'block_area_m2', 'calibration',
                'signatures', 'plots')
    missing = [k for k in required if k not in doc]
    if missing:
        raise ValueError('mixture_fields_missing:' + ','.join(missing))
    if doc['kind'] != 'fuel_mixture':
        raise ValueError('source_kind_or_id')
    cal = doc['calibration']
    for key in ('bias_bounds', 'bands'):
        if key not in cal:
            raise ValueError('calibration_fields_missing')
    if len(cal['bands']) != 3:
        raise ValueError('three_band_calibration_required')
    bounds = cal['bias_bounds']
    if not isinstance(bounds, dict) or len(bounds.get('lower', [])) != 3 \
            or len(bounds.get('upper', [])) != 3:
        raise ValueError('three_band_calibration_required')
    materials = []
    for sig in doc['signatures']:
        for key in ('material', 'bands', 'dry_mass_per_m2'):
            if key not in sig:
                raise ValueError('signature_fields_missing')
        if len(sig['bands']) != 3:
            raise ValueError('three_band_signature_required')
        materials.append(sig['material'])
    if len(set(materials)) != len(materials):
        raise ValueError('duplicate_material')
    for plot in doc['plots']:
        for key in ('plot_id', 'area_m2', 'bands', 'band_error', 'shade'):
            if key not in plot:
                raise ValueError('plot_fields_missing')
        if len(plot['bands']) != 3 or len(plot['band_error']) != 3:
            raise ValueError('three_band_plot_required')
        shade = plot['shade']
        if not isinstance(shade, dict) or 'fraction' not in shade or 'tolerance' not in shade:
            raise ValueError('shade_interval_required')


def _build_baseline(doc):
    """Construct the shared-bias, shade-bounded three-band feasibility LP.

    Variables per plot p and material m: cover fraction f[p][m] >= 0;
    shade fraction h[p]; per-band positive/negative residual u[p][b], v[p][b].
    Globally: one calibration bias d[b] per band, shared by every plot.
    The forward model is
        obs[p][b] = sum_m f[p][m] * ref[m][b] + h[p]*0 + d[b] + u - v
    with |u-v| bounded by the instrument band error, so a feasible point
    exists exactly when the observation is explainable within tolerance.
    """
    model = _Model()
    sigs = doc['signatures']
    materials = [s['material'] for s in sigs]
    refs = [[F(x) for x in s['bands']] for s in sigs]
    mass_per_m2 = {s['material']: F(s['dry_mass_per_m2']) for s in sigs}
    cal = doc['calibration']
    bias_lo = [F(x) for x in cal['bias_bounds']['lower']]
    bias_hi = [F(x) for x in cal['bias_bounds']['upper']]

    bias = [model.var(f'bias_b{b}', bias_lo[b], bias_hi[b]) for b in range(3)]

    plot_vars = {}
    for plot in doc['plots']:
        pid = plot['plot_id']
        frac = {m: model.var(f'{pid}.fraction.{m}') for m in materials}
        shade = F(plot['shade']['fraction'])
        shade_tol = F(plot['shade']['tolerance'])
        h = model.var(f'{pid}.shade', max(Fraction(0), shade - shade_tol), shade + shade_tol)
        # cover fractions and shade partition the pixel
        model.constrain([(idx, Fraction(1)) for idx in list(frac.values())]
                        + [(h, Fraction(1))], '=', Fraction(1))
        u, v = [], []
        obs = [F(x) for x in plot['bands']]
        err = [F(x) for x in plot['band_error']]
        for b in range(3):
            ub = model.var(f'{pid}.resid_pos_b{b}', 0, err[b])
            vb = model.var(f'{pid}.resid_neg_b{b}', 0, err[b])
            u.append(ub); v.append(vb)
            terms = [(frac[m], refs[k][b]) for k, m in enumerate(materials)]
            terms += [(bias[b], Fraction(1)), (ub, Fraction(1)), (vb, Fraction(-1))]
            model.constrain(terms, '=', obs[b])
        plot_vars[pid] = {'fraction': frac, 'shade': h, 'resid_pos': u, 'resid_neg': v,
                          'area_m2': F(plot['area_m2'])}
    return model, materials, refs, mass_per_m2, bias, plot_vars


def _signature_dependence(refs, materials):
    """Find materials whose reference is an exact convex combination of others.

    Exact rational rank comparison on the augmented reference matrix.  A
    dependent material cannot be separated on three bands alone, so its
    fraction is intrinsically an interval rather than a point.
    """
    dependent = []
    for k, material in enumerate(materials):
        other_mats = [m for j, m in enumerate(materials) if j != k]
        others = [r for j, r in enumerate(refs) if j != k]
        target = refs[k]
        n = len(others)
        # convex hull: find non-negative weights summing to 1 whose weighted
        # average of the other references equals the target exactly
        A_eq = [[others[j][b] for j in range(n)] for b in range(3)]
        A_eq.append([Fraction(1)] * n)
        b_eq = list(target) + [Fraction(1)]
        res = linprog([0] * n, A_eq=A_eq, b_eq=b_eq, bounds=[(0, 1)] * n)
        if res['status'] != 'optimal':
            continue
        weights = res['x']
        witness = [other_mats[j] for j, w in enumerate(weights) if w > 0]
        dependent.append({'material': material,
                          'convex_components': witness,
                          'weights': [str(w) for w in weights if w > 0]})
    return dependent


def _interval_width(a, b):
    return b - a


def analyze_mixture(doc, band4=None, clipping=None):
    """Compute provable fraction/mass/load intervals for one transect.

    ``band4`` and ``clipping`` are the two approved additional-evidence
    documents.  They are never combined: each route starts from the same
    three-band observation and changes a different part of the answer, so
    the caller can see precisely what each kind of evidence can and cannot
    prove.
    """
    _validate_mixture(doc)
    if band4 is not None and clipping is not None:
        raise ValueError('evidence_routes_cannot_be_combined')
    model, materials, refs, mass_per_m2, bias, plot_vars = _build_baseline(doc)

    stage = 'baseline'
    band4_evidence = band4 is not None
    clipping_evidence = clipping is not None
    clip_bounds = None
    if band4_evidence:
        stage = 'band4'
        _apply_band4(model, doc, band4, materials, plot_vars, bias)
    if clipping_evidence:
        stage = 'clipping'
        clip_bounds = _apply_clipping(model, clipping, materials, plot_vars, mass_per_m2)

    dependence = _signature_dependence(refs, materials)

    # joint minimum total residual across the whole transect (single shared
    # calibration, one consistent bias vector for every plot)
    resid_index = set()
    for pv in plot_vars.values():
        resid_index.update(pv['resid_pos'])
        resid_index.update(pv['resid_neg'])
    residual_objective = [Fraction(1) if i in resid_index else Fraction(0)
                          for i in range(len(model.var_name))]
    total_resid = model.solve(residual_objective)
    if total_resid['status'] != 'optimal':
        raise ValueError('model_infeasible')

    plots_out = []
    exact_tol = Fraction(1, 10 ** 6)
    severe_shade = F(doc.get('severe_shade_threshold', '0.2'))
    for plot in doc['plots']:
        pid = plot['plot_id']
        pv = plot_vars[pid]
        area = pv['area_m2']
        fraction_intervals, mass_intervals = {}, {}
        identifiable = True
        for m in materials:
            flo, fhi = model.extrema(pv['fraction'][m])
            fraction_intervals[m] = [str(flo), str(fhi)]
            # mass = cover fraction * area * dry mass per m2 of that material
            mlo = flo * area * mass_per_m2[m]
            mhi = fhi * area * mass_per_m2[m]
            mass_intervals[m] = [str(mlo), str(mhi)]
            if _interval_width(flo, fhi) > exact_tol:
                identifiable = False
        slo, shi = model.extrema(pv['shade'])
        # joint per-plot residual: one aggregate of the non-negative residual
        # variables (two LP solves), instead of six independent extrema whose
        # loose sum would overstate the achievable residual range
        resid_agg = model.var(f'{pid}.residual_total')
        model.constrain([(resid_agg, Fraction(1))]
                        + [(idx, Fraction(-1))
                           for idx in pv['resid_pos'] + pv['resid_neg']],
                        '=', Fraction(0))
        r_lo, r_hi = model.extrema(resid_agg)
        shade_fraction = F(plot['shade']['fraction'])
        plots_out.append({
            'plot_id': pid,
            'area_m2': str(area),
            'fraction_interval': fraction_intervals,
            'mass_interval_kg': mass_intervals,
            'shade_interval': [str(slo), str(shi)],
            'residual_interval': [str(r_lo), str(r_hi)],
            'identifiable': identifiable,
            'severe_shade': shade_fraction >= severe_shade,
            'non_identifiable_materials': sorted(
                m for m in materials
                if _interval_width(*[F(x) for x in fraction_intervals[m]]) > exact_tol),
        })

    # group-level mass intervals (jointly optimized, not summed per-plot
    # extrema which would pair incompatible biases and overstate the range)
    group_mass = {}
    for group, members in (('pine_litter', ['pine_litter']),
                           ('grass', ['grass']),
                           ('other', [m for m in materials if m not in ('pine_litter', 'grass')])):
        if not members:
            continue
        rows = []
        for m in members:
            for pid, pv in plot_vars.items():
                rows.append((pv['fraction'][m], pv['area_m2'] * mass_per_m2[m]))
        agg = model.var(f'__agg_{group}')
        model.constrain([(agg, Fraction(1))] + [(i, -c) for i, c in rows], '=', Fraction(0))
        glo, ghi = model.extrema(agg)
        group_mass[group] = [str(glo), str(ghi)]

    bias_interval = [list(map(str, model.extrema(b))) for b in bias]
    block_area = F(doc['block_area_m2'])
    sampled_area = sum(pv['area_m2'] for pv in plot_vars.values())

    # conservative compartment load bounds: every feasible composition,
    # one shared bias, and an explicit coverage statement for unsampled area
    total_mass_rows = [(pv['fraction'][m], pv['area_m2'] * mass_per_m2[m])
                       for pv in plot_vars.values() for m in materials]
    total_var = model.var('__total_sampled_mass')
    model.constrain([(total_var, Fraction(1))] + [(i, -c) for i, c in total_mass_rows],
                    '=', Fraction(0))
    sampled_lo, sampled_hi = model.extrema(total_var)

    # Conservative whole-compartment load bound.  The lower bound only counts
    # fuel that the feasible model already requires inside sampled plots;
    # nothing is assumed about unsampled ground, so its fuel can be zero.
    # If the protocol supplies a physical maximum dry mass per m2, the upper
    # bound caps the unsampled area; without it the upper bound is open and
    # the result says so rather than fabricating a number.
    density_cap = doc.get('max_dry_mass_per_m2')
    block_load = {
        'lower_bound_kg': str(sampled_lo),
        'upper_bound_kg': None,
        'unsampled_area_m2': str(block_area - sampled_area),
        'note': 'lower bound covers required sampled fuel only; unsampled '
                'area contributes no assumed mass',
    }
    if density_cap is not None:
        cap = F(density_cap)
        upper = sampled_hi + (block_area - sampled_area) * cap
        block_load['upper_bound_kg'] = str(upper)
        block_load['note'] = ('upper bound = sampled feasible maximum plus '
                              'physical density cap over unsampled area')

    coverage = _coverage(doc, plots_out, stage, clip_bounds if clipping_evidence else None)

    return {
        'block_id': doc['block_id'],
        'stage': stage,
        'materials': materials,
        'non_identifiable_signatures': dependence,
        'plots': plots_out,
        'group_mass_interval_kg': group_mass,
        'joint_min_total_residual': str(total_resid['fun']),
        'calibration_bias_interval': bias_interval,
        'block_area_m2': str(block_area),
        'sampled_area_m2': str(sampled_area),
        'unsampled_area_m2': str(block_area - sampled_area),
        'sampled_fuel_load_interval_kg': [str(sampled_lo), str(sampled_hi)],
        'load_per_sampled_m2_interval': [str(sampled_lo / sampled_area),
                                         str(sampled_hi / sampled_area)],
        'block_load_conservative': block_load,
        'coverage': coverage,
        'precision_guard': _precision_guard(dependence, plots_out, stage),
    }


def _apply_band4(model, doc, band4, materials, plot_vars, bias):
    """Add the fourth-band separation constraints, plot by plot.

    A fourth reference value per material plus its own band error makes the
    wet-leaf/pine+grass ambiguity resolvable where shade does not dominate.
    Severely shaded cells keep their three-band intervals (the extra band
    cannot recover signal under shade) and stay on the resurvey list.
    """
    if band4.get('kind') != 'band4_evidence' or band4.get('mixture_id') != doc['id']:
        raise ValueError('band4_identity_mismatch')
    refs4 = {s['material']: F(s['band4']) for s in band4['signatures']}
    if set(refs4) != set(materials):
        raise ValueError('band4_signature_mismatch')
    for obs in band4['plots']:
        pid = obs['plot_id']
        if pid not in plot_vars:
            raise ValueError('band4_unknown_plot')
        pv = plot_vars[pid]
        if F(doc_plot(doc, pid)['shade']['fraction']) >= F(doc.get('severe_shade_threshold', '0.2')):
            continue  # shade-dominated: fourth band adds no separable signal
        b4 = model.var(f'{pid}.band4_bias',
                       F(band4['calibration']['bias_bounds']['lower']),
                       F(band4['calibration']['bias_bounds']['upper']))
        up = model.var(f'{pid}.band4_resid_pos', 0, F(obs['band4_error']))
        vn = model.var(f'{pid}.band4_resid_neg', 0, F(obs['band4_error']))
        terms = [(pv['fraction'][m], refs4[m]) for m in materials]
        terms += [(b4, Fraction(1)), (up, Fraction(1)), (vn, Fraction(-1))]
        model.constrain(terms, '=', F(obs['bands'][3]))


def doc_plot(doc, pid):
    return next(p for p in doc['plots'] if p['plot_id'] == pid)


def _apply_clipping(model, clipping, materials, plot_vars, mass_per_m2):
    """Constrain total plot dry mass from two-team fixed-area clipping.

    Two distinct crews harvest and weigh the same fixed area.  The pair of
    weighings brackets the true dry mass; a disagreement beyond the declared
    tolerance means the evidence is internally contradictory and is rejected.
    Clipping only binds the clipped cells — nothing is extrapolated.
    """
    if clipping.get('kind') != 'clipping_evidence':
        raise ValueError('clipping_kind_invalid')
    bounds = {}
    teams = set()
    for cell in clipping['cells']:
        pid = cell['plot_id']
        if pid not in plot_vars:
            raise ValueError('clipping_unknown_plot')
        weighings = []
        for w in cell['weighings']:
            teams.add(w['team'])
            weighings.append(F(w['dry_mass_kg']))
        if len(weighings) != 2:
            raise ValueError('clipping_requires_two_teams')
        lo, hi = sorted(weighings)
        if hi - lo > F(cell['agreement_tolerance']):
            raise ValueError('clipping_team_disagreement')
        bounds[pid] = (lo, hi, F(cell['clipped_area_m2']))
    if len(teams) < 2:
        raise ValueError('clipping_requires_two_teams')
    # The harvested mass covers the clipped sub-area only.  Assuming the
    # clipped sub-area carries the same per-m2 composition as the plot
    # fractions, its mass = sum_m fraction * clipped_area * mass/m2.  That
    # bracketed total is the only dry-weight figure treated as confirmed;
    # it never scales up to the full plot or the transect.
    for pid, (lo, hi, clipped_area) in bounds.items():
        pv = plot_vars[pid]
        total = model.var(f'{pid}.clipped_mass')
        model.constrain([(total, Fraction(1))], '>=', lo)
        model.constrain([(total, Fraction(1))], '<=', hi)
        model.constrain([(total, Fraction(1))]
                        + [(pv['fraction'][m], -clipped_area * mass_per_m2[m])
                           for m in materials],
                        '=', Fraction(0))
    return bounds


def _coverage(doc, plots_out, stage, clip_bounds):
    severe = [p['plot_id'] for p in plots_out if p['severe_shade']]
    if stage == 'band4':
        continuous = [p['plot_id'] for p in plots_out if not p['severe_shade']]
        return {
            'continuous_coverage_plots': continuous,
            'continuous_coverage_area_m2': str(sum(
                F(doc_plot(doc, pid)['area_m2']) for pid in continuous)),
            'confirmed_dry_mass_kg': '0',
            'confirmed_area_m2': '0',
            'pending_resurvey_plots': severe,
            'note': 'fourth band yields continuous fractions outside shade; '
                    'it is optical evidence and never replaces dry-weight proof',
        }
    if stage == 'clipping':
        clipped = list(clip_bounds)
        confirmed_area = sum(clip_bounds[pid][2] for pid in clipped)
        confirmed_mass_lo = sum(clip_bounds[pid][0] for pid in clipped)
        confirmed_mass_hi = sum(clip_bounds[pid][1] for pid in clipped)
        clipped_set = set(clipped)
        pending = [p['plot_id'] for p in plots_out
                   if p['plot_id'] not in clipped_set or p['severe_shade']]
        return {
            'continuous_coverage_plots': [],
            'continuous_coverage_area_m2': '0',
            'confirmed_dry_mass_kg': [str(confirmed_mass_lo), str(confirmed_mass_hi)],
            'confirmed_area_m2': str(confirmed_area),
            'pending_resurvey_plots': pending,
            'note': 'dry mass is confirmed only inside clipped cells; '
                    'no extrapolation to unclipped cells or the full transect',
        }
    return {
        'continuous_coverage_plots': [],
        'continuous_coverage_area_m2': '0',
        'confirmed_dry_mass_kg': '0',
        'confirmed_area_m2': '0',
        'pending_resurvey_plots': [p['plot_id'] for p in plots_out],
        'note': 'three bands alone cannot identify the mixture or prove dry mass',
    }


def _precision_guard(dependence, plots_out, stage):
    """State explicitly why a precise percentage must not be reported."""
    reasons = []
    if dependence:
        reasons.append('reference_spectra_linearly_dependent:'
                       + ','.join(d['material'] for d in dependence))
    if any(p['severe_shade'] for p in plots_out):
        reasons.append('shade_not_separable_in_severe_cells')
    reasons.append('shared_calibration_bias_jointly_constrained')
    if stage == 'baseline':
        reasons.append('no_point_percentage_from_three_bands')
    return reasons


# ---------------------------------------------------------------------------
# ledger integration (import + persistence), separate from legacy scan tables
# ---------------------------------------------------------------------------

import json
from contextlib import closing

from .store import connect, source


def _persist(db, source_id, result):
    stage = result['stage']
    db.execute('DELETE FROM unmix_plot WHERE source_id=? AND stage=?', (source_id, stage))
    db.execute('DELETE FROM unmix_block WHERE source_id=? AND stage=?', (source_id, stage))
    for plot in result['plots']:
        db.execute(
            'INSERT INTO unmix_plot VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (source_id, stage, plot['plot_id'], plot['area_m2'],
             json.dumps(plot['fraction_interval']), json.dumps(plot['mass_interval_kg']),
             json.dumps(plot['shade_interval']), json.dumps(plot['residual_interval']),
             1 if plot['identifiable'] else 0, 1 if plot['severe_shade'] else 0,
             json.dumps(plot['non_identifiable_materials'])))
    db.execute(
        'INSERT INTO unmix_block VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (source_id, stage, result['block_id'], json.dumps(result['materials']),
         json.dumps(result['non_identifiable_signatures']),
         json.dumps(result['group_mass_interval_kg']),
         result['joint_min_total_residual'],
         json.dumps(result['calibration_bias_interval']),
         result['block_area_m2'], result['sampled_area_m2'], result['unsampled_area_m2'],
         json.dumps(result['sampled_fuel_load_interval_kg']),
         json.dumps(result['load_per_sampled_m2_interval']),
         json.dumps(result['coverage'], ensure_ascii=False),
         json.dumps(result['precision_guard'], ensure_ascii=False)))


def import_mixture(db_path, file):
    """Register a mixed-scan source and store its baseline interval analysis."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'fuel_mixture')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        db.execute('INSERT INTO fuel_mixture VALUES(?,?,?)',
                   (doc['id'], doc['block_id'], json.dumps(doc, ensure_ascii=False)))
        result = analyze_mixture(doc)
        _persist(db, doc['id'], result)
        return {'id': doc['id'], 'stage': 'baseline',
                'non_identifiable': [d['material']
                                     for d in result['non_identifiable_signatures']],
                'sampled_fuel_load_interval_kg': result['sampled_fuel_load_interval_kg']}


def import_band4(db_path, file):
    """Add fourth-band evidence and recompute the band4 stage (no overwrite)."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'band4_evidence')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        mixture_row = db.execute('SELECT doc FROM fuel_mixture WHERE source_id=?',
                                 (doc['mixture_id'],)).fetchone()
        if mixture_row is None:
            raise ValueError('mixture_not_found')
        mixture = json.loads(mixture_row['doc'])
        db.execute('INSERT INTO fuel_evidence VALUES(?,?,?,?)',
                   (doc['id'], doc['mixture_id'], 'band4_evidence',
                    json.dumps(doc, ensure_ascii=False)))
        result = analyze_mixture(mixture, band4=doc)
        _persist(db, doc['mixture_id'], result)
        return {'id': doc['id'], 'stage': 'band4',
                'continuous_coverage_area_m2':
                    result['coverage']['continuous_coverage_area_m2'],
                'pending_resurvey_plots': result['coverage']['pending_resurvey_plots']}


def import_clipping(db_path, file):
    """Add two-team clipping evidence and recompute the clipping stage."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'clipping_evidence')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        mixture_row = db.execute('SELECT doc FROM fuel_mixture WHERE source_id=?',
                                 (doc['mixture_id'],)).fetchone()
        if mixture_row is None:
            raise ValueError('mixture_not_found')
        mixture = json.loads(mixture_row['doc'])
        db.execute('INSERT INTO fuel_evidence VALUES(?,?,?,?)',
                   (doc['id'], doc['mixture_id'], 'clipping_evidence',
                    json.dumps(doc, ensure_ascii=False)))
        result = analyze_mixture(mixture, clipping=doc)
        _persist(db, doc['mixture_id'], result)
        return {'id': doc['id'], 'stage': 'clipping',
                'confirmed_area_m2': result['coverage']['confirmed_area_m2'],
                'confirmed_dry_mass_kg': result['coverage']['confirmed_dry_mass_kg'],
                'pending_resurvey_plots': result['coverage']['pending_resurvey_plots']}
