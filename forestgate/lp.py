"""Exact rational two-phase simplex solver, standard library only.

All coefficients are converted to :class:`fractions.Fraction` so that
degenerate optima (several mixtures sharing residual zero) are never
silently collapsed to a single floating point answer.  The problems built
by :mod:`forestgate.unmix` are small (a handful of field plots), so exact
arithmetic is comfortably fast.
"""
from fractions import Fraction

# Process-level memoization of solved problems.  Interval analysis solves the
# same constraint system repeatedly with one-objective-at-a-time changes; the
# cache turns those repeats into lookups without weakening exact arithmetic.
_LP_CACHE = {}
_LP_CACHE_MAX = 1024


def _f(x):
    if isinstance(x, Fraction):
        return x
    if isinstance(x, bool):
        raise ValueError('numeric_required')
    return Fraction(str(x))


def linprog(c, A_ub=None, b_ub=None, A_eq=None, b_eq=None, bounds=None):
    """Cached wrapper around :func:`_linprog_core` (exact rationals)."""
    n = len(c)
    if bounds is None:
        bounds = [(0, None)] * n
    key = (
        tuple(str(_f(v)) for v in c),
        tuple(tuple(str(_f(a)) for a in row) for row in (A_ub or [])),
        tuple(str(_f(v)) for v in (b_ub or [])),
        tuple(tuple(str(_f(a)) for a in row) for row in (A_eq or [])),
        tuple(str(_f(v)) for v in (b_eq or [])),
        tuple((str(Fraction(0) if lb is None else _f(lb)),
               None if ub is None else str(_f(ub))) for lb, ub in bounds),
        n,
    )
    hit = _LP_CACHE.get(key)
    if hit is not None:
        status, fun, x = hit
        return {'status': status, 'x': list(x) if x is not None else None, 'fun': fun}
    result = _linprog_core(c, A_ub, b_ub, A_eq, b_eq, bounds)
    if len(_LP_CACHE) >= _LP_CACHE_MAX:
        _LP_CACHE.clear()
    _LP_CACHE[key] = (result['status'], result['fun'],
                      tuple(result['x']) if result['x'] is not None else None)
    return result


def _linprog_core(c, A_ub=None, b_ub=None, A_eq=None, b_eq=None, bounds=None):
    """Minimize ``c·x`` subject to linear constraints.

    ``bounds`` is a list of ``(lower, upper)`` pairs; ``None`` means
    unbounded on that side and the default lower bound is zero.

    Returns ``{"status": "optimal", "x": [...Fraction], "fun": Fraction}``
    or ``status`` ``"infeasible"`` / ``"unbounded"``.
    """
    n = len(c)
    c = [_f(v) for v in c]
    if bounds is None:
        bounds = [(0, None)] * n
    lower = []
    for j, (lb, ub) in enumerate(bounds):
        lower.append(Fraction(0) if lb is None else _f(lb))
    rows = []  # (coefficient list, sense, rhs) in shifted variables x'=x-lower
    for j, (lb, ub) in enumerate(bounds):
        if ub is not None:
            ub = _f(ub)
            row = [Fraction(0)] * n
            row[j] = Fraction(1)
            rows.append((row, '<=', ub - lower[j]))

    def add(A, b, sense):
        if A is None:
            return
        for raw_row, raw_rhs in zip(A, b):
            row = [_f(v) for v in raw_row]
            # substitute x = x' + lower: a.x <= b becomes a.x' <= b - a.lower
            rhs = _f(raw_rhs) - sum(row[j] * lower[j] for j in range(n))
            this_sense = sense
            if rhs < 0:  # keep RHS non-negative for the basis setup
                row = [-v for v in row]
                rhs = -rhs
                this_sense = '>=' if this_sense == '<=' else ('<=' if this_sense == '>=' else '=')
            rows.append((row, this_sense, rhs))

    add(A_ub, b_ub, '<=')
    add(A_eq, b_eq, '=')

    n_struct = n
    slack_col = {}
    next_slack = n_struct
    for i, (_, sense, _) in enumerate(rows):
        if sense != '=':
            slack_col[i] = next_slack
            next_slack += 1
    n_slack = len(slack_col)
    art_rows = [i for i, (_, sense, _) in enumerate(rows) if sense in ('>=', '=')]
    n_art = len(art_rows)
    n_cols = n_struct + n_slack + n_art
    rhs_col = n_cols
    tableau = [[Fraction(0) for _ in range(n_cols + 1)] for _ in range(len(rows) + 1)]
    basis = []
    art_index = {row_i: n_struct + n_slack + k for k, row_i in enumerate(art_rows)}
    for i, (row, sense, rhs) in enumerate(rows):
        t = tableau[i + 1]
        for j, v in enumerate(row):
            t[j] = v
        if i in slack_col:
            # '<=' surplus slack is a valid starting basis variable;
            # the '>=' surplus column starts non-basic at zero
            t[slack_col[i]] = Fraction(-1) if sense == '>=' else Fraction(1)
            if sense == '<=':
                basis.append(slack_col[i])
        if i in art_index:
            # every '>=' or '=' row gets an artificial variable that IS
            # the starting basis variable for that row
            t[art_index[i]] = Fraction(1)
            basis.append(art_index[i])
        t[rhs_col] = rhs
    assert len(basis) == len(rows)

    def pivot(tab, base, ncols, rcol, enter_limit=None):
        """Bland-rule simplex on objective row 0.

        ``enter_limit`` restricts entering columns to indices below it;
        Phase I passes the first artificial-column index so artificial
        variables can never re-enter the basis.
        """
        limit = ncols if enter_limit is None else enter_limit
        while True:
            enters = [j for j in range(limit) if tab[0][j] < 0]
            if not enters:
                return 'optimal'
            enter = min(enters)
            best = None
            for i in range(1, len(tab)):
                if tab[i][enter] > 0:
                    ratio = tab[i][rcol] / tab[i][enter]
                    if best is None or ratio < best[0] or (ratio == best[0] and base[i - 1] < best[1]):
                        best = (ratio, base[i - 1], i)
            if best is None:
                return 'unbounded'
            leave_row = best[2]
            pivot_val = tab[leave_row][enter]
            tab[leave_row] = [v / pivot_val for v in tab[leave_row]]
            for i in range(len(tab)):
                if i == leave_row or tab[i][enter] == 0:
                    continue
                factor = tab[i][enter]
                tab[i] = [v - factor * tab[leave_row][k] for k, v in enumerate(tab[i])]
            base[leave_row - 1] = enter

    # ---- Phase I: minimize the sum of artificial variables ----
    if art_rows:
        phase1 = [row[:] for row in tableau]
        art_cols = list(art_index.values())
        art_set = set(art_cols)
        starting_basic = set(basis)
        # Build the Phase-I objective row from the explicit formula for
        # each basic artificial variable.  From artificial row i (art
        # column coefficient +1): a_i = rhs_i - sum_j coeff_ij * var_j,
        # and w = sum_i a_i.  Row 0 stores w directly, so its entry for a
        # non-basic variable is minus the sum of that column's coefficients
        # across artificial rows and its RHS is the sum of their RHS.
        # Every currently basic column (artificial or '>=' surplus) must
        # stay zero, otherwise the starting point is not canonical.
        obj_row = phase1[0]
        for row_i in art_rows:
            src = tableau[row_i + 1]
            for j in range(rhs_col):
                if j in starting_basic:
                    continue
                obj_row[j] -= src[j]
            obj_row[rhs_col] += src[rhs_col]
        first_art = n_struct + n_slack
        if pivot(phase1, basis, n_cols, rhs_col, first_art) != 'optimal':
            return {'status': 'unbounded', 'x': None, 'fun': None}
        # feasibility verdict: read artificial variables from the final
        # basis rather than trusting row-0 RHS across degenerate pivots
        art_value = sum(phase1[row_idx][rhs_col]
                        for row_idx, b in enumerate(basis, start=1) if b in art_set)
        if art_value > 0:
            return {'status': 'infeasible', 'x': None, 'fun': None}
        tableau = phase1
        # an artificial may still sit in the basis at zero level
        # (degenerate rows); swap it for any non-artificial column
        for i in range(1, len(tableau)):
            if basis[i - 1] not in art_set:
                continue
            swap = next((j for j in range(n_struct + n_slack) if tableau[i][j] != 0), None)
            if swap is None:
                tableau[i] = [Fraction(0)] * (n_cols + 1)  # redundant row
                basis[i - 1] = -1
                continue
            pivot_val = tableau[i][swap]
            tableau[i] = [v / pivot_val for v in tableau[i]]
            for k in range(len(tableau)):
                if k == i or tableau[k][swap] == 0:
                    continue
                factor = tableau[k][swap]
                tableau[k] = [v - factor * tableau[i][j] for j, v in enumerate(tableau[k])]
            basis[i - 1] = swap
        live = [i for i in range(1, len(tableau)) if basis[i - 1] >= 0]
        tableau = [tableau[0]] + [tableau[i] for i in live]
        basis = [basis[i - 1] for i in live]
        # drop artificial columns before reusing the tableau in Phase II
        keep = [j for j in range(n_cols) if j not in art_set] + [rhs_col]
        tableau = [[row[j] for j in keep] for row in tableau]
        n_cols = n_struct + n_slack
        rhs_col = n_cols

    # ---- Phase II: the real objective on shifted variables ----
    objective = [Fraction(0)] * (n_cols + 1)
    for j, v in enumerate(c):
        objective[j] = v
    const = sum(c[j] * lower[j] for j in range(n))
    tableau[0] = objective
    # zero the reduced costs of the current basic variables
    for i, bvar in enumerate(basis):
        factor = tableau[0][bvar]
        if factor:
            tableau[0] = [v - factor * tableau[i + 1][k] for k, v in enumerate(tableau[0])]
    outcome = pivot(tableau, basis, n_cols, rhs_col)
    if outcome == 'unbounded':
        return {'status': 'unbounded', 'x': None, 'fun': None}
    values = [Fraction(0)] * n_struct
    for i, bvar in enumerate(basis):
        if bvar < n_struct:
            values[bvar] = tableau[i + 1][rhs_col]
    x = [lower[j] + values[j] for j in range(n)]
    return {'status': 'optimal', 'x': x, 'fun': -tableau[0][rhs_col] + const}


def feasible(A_ub=None, b_ub=None, A_eq=None, b_eq=None, bounds=None, n=None):
    """Return True iff the constraint system has any feasible point."""
    if n is None:
        n = len(bounds)
    res = linprog([0] * n, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds)
    return res['status'] == 'optimal'
