"""Minimal stdlib-only pytest shim for offline environments without pytest.

Supports the small subset used by this repository's tests:
tmp_path fixture, pytest.raises, pytest.approx. Run with:
    PYTHONPATH=/workspace/.pyshim python3 -m pytest -q
"""
import re
import sys
import tempfile
import traceback
from pathlib import Path


class Approx:
    def __init__(self, expected, rel=1e-6, abs_=1e-9):
        self.expected = expected
        self.rel = rel
        self.abs = abs_

    def __eq__(self, other):
        return abs(other - self.expected) <= max(
            self.rel * max(abs(other), abs(self.expected)), self.abs)

    def __repr__(self):
        return f'approx({self.expected!r})'


def approx(expected, rel=1e-6, abs=1e-9):  # noqa: A002 - mirror pytest API
    return Approx(expected, rel, abs)


class Raises:
    def __init__(self, pattern=None):
        self.pattern = pattern
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f'DID NOT RAISE (expected /{self.pattern}/)')
        self.value = exc
        if self.pattern is not None and not re.search(self.pattern, str(exc)):
            raise AssertionError(
                f'pattern {self.pattern!r} not found in {str(exc)!r}')
        return True


def raises(exc_type, match=None):
    return Raises(match)


def _tmp_path():
    return Path(tempfile.mkdtemp(prefix='pyshim-'))


def main(argv):
    root = Path(__file__).resolve().parents[2]
    test_files = sorted(root.glob('tests/test_*.py'))
    passed = failed = 0
    failures = []
    for file in test_files:
        ns = {'__file__': str(file), '__name__': file.stem}
        sys.path.insert(0, str(root))
        try:
            code = compile(file.read_text(encoding='utf-8'), str(file), 'exec')
            exec(code, ns)
        except Exception:
            failed += 1
            failures.append((f'{file.name}::import', traceback.format_exc()))
            continue
        for name, fn in sorted(ns.items()):
            if not name.startswith('test_') or not callable(fn):
                continue
            try:
                if 'tmp_path' in fn.__code__.co_varnames:
                    fn(_tmp_path())
                else:
                    fn()
                passed += 1
            except Exception:
                failed += 1
                failures.append((f'{file.name}::{name}', traceback.format_exc()))
    for label, tb in failures:
        print(f'FAIL {label}\n{tb}', file=sys.stderr)
    print(f'{passed} passed, {failed} failed')
    return 1 if failed else 0
