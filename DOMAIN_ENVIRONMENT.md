# Reproduce the data-engineering environment

Source: https://github.com/kevin1024/vcrpy/commit/10ee52eb6ff2842ec6c60a50eb8c800c93664add

This repository republishes the tracked files of that upstream commit under their original licenses.
Upstream CI workflows are omitted from the evaluation snapshot. This commit adds environment instructions
and the dependency lock only. The source license is MIT; original license and attribution files remain.

Python 3.13.15 and uv were used on Linux. From a fresh checkout:

```sh
uv venv --python 3.13.15 .venv
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
uv pip install --python .venv/bin/python -r requirements-domain.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps -e .
.venv/bin/python -B -m pytest -q -o addopts= -p no:cacheprovider tests/unit/test_cassettes.py tests/unit/test_errors.py tests/unit/test_filters.py tests/unit/test_httpx_stubs.py tests/unit/test_matchers.py tests/unit/test_migration.py tests/unit/test_persist.py tests/unit/test_request.py tests/unit/test_response.py tests/unit/test_serialize.py
```

The selected tests use small fixtures or in-process substitutes. They need no remote service after dependency setup.
Start a fresh checkout and rebuild its virtual environment to restore initial state.
