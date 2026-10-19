# Third-party notices

The project source is licensed under AGPL-3.0-only; see [LICENSE](LICENSE).
Dependencies retain their own copyrights and license terms. They are installed
by `uv` from the inline dependency declarations, not vendored into this repository.

## Runtime dependencies

| Dependency | License overview | Upstream |
| --- | --- | --- |
| NumPy | BSD-3-Clause; distributions include additional licensed components | https://numpy.org/doc/stable/license.html |
| OpenCV / opencv-python | Apache-2.0; binary wheels may contain additional third-party components | https://github.com/opencv/opencv-python |
| Pillow | MIT-CMU for current supported releases; consult the license of the installed version | https://github.com/python-pillow/Pillow |
| pillow-heif | BSD-3-Clause for the Python wrapper; native libraries/codecs have their own terms | https://github.com/bigcat88/pillow_heif |
| ReportLab | BSD-style license | https://pypi.org/project/reportlab/ |
| Flask | BSD-3-Clause | https://github.com/pallets/flask |
| PyMuPDF / MuPDF | AGPL-3.0 or a separate commercial license from Artifex | https://pymupdf.readthedocs.io/en/latest/about.html |

The watermark implementation uses PyMuPDF's bundled CJK font at runtime and
embeds a subset into generated PDFs. This repository does not ship a separate
font binary. The bundled font's own license and notices remain applicable;
consult the PyMuPDF/MuPDF distribution for those notices.

## Test-only dependencies

| Dependency | License | Upstream |
| --- | --- | --- |
| pypdf | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| Playwright | Apache-2.0 | https://github.com/microsoft/playwright-python |

Browser smoke tests use a separately installed browser; browser licenses are
independent of Playwright's license.

This is an informational overview, not a complete inventory of transitive
packages, codecs, native libraries, or wheel contents. Check the exact installed
versions and their bundled notices when redistributing dependencies or building
a packaged application. Nothing in this file relicenses third-party code.
