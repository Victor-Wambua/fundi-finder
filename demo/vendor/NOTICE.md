# Third-party components served with the demo

The demo runs Python in the browser. These files are copied unchanged from the official
Pyodide 0.27.7 release (npm package `pyodide@0.27.7` and its CDN package set) so the page makes
no request to any other site. Each wheel's SHA-256 matches `pyodide-lock.json`, and Pyodide
checks it again when the wheel is loaded.

| Component | Version | Licence | Files |
|---|---|---|---|
| Pyodide runtime (CPython 3.12.7 built for WebAssembly) | 0.27.7 | MPL-2.0 ([text](licenses/pyodide-MPL-2.0.txt)) | `pyodide/pyodide.js`, `pyodide.asm.js`, `pyodide.asm.wasm`, `pyodide-lock.json` |
| CPython standard library | 3.12.7 | PSF License ([python.org/psf/license](https://docs.python.org/3/license.html)) | `pyodide/python_stdlib.zip` |
| SQLAlchemy | 2.0.29 | MIT ([text](licenses/sqlalchemy-MIT.txt)) | `pyodide/sqlalchemy-2.0.29-cp312-cp312-pyodide_2024_0_wasm32.whl` |
| sqlite3 module (links SQLite, public domain) | 1.0.0 | PSF License; SQLite is public domain | `pyodide/sqlite3-1.0.0-cp312-cp312-pyodide_2024_0_wasm32.whl` |
| typing_extensions | 4.11.0 | PSF License ([text](licenses/typing-extensions-PSF.txt)) | `pyodide/typing_extensions-4.11.0-py3-none-any.whl` |

Fundi Finder's own code is MIT licensed (see the repository's `LICENSE`).

To update: download the same files for a newer Pyodide release, keep `pyodide-lock.json` from
that release, and run the demo tests.
