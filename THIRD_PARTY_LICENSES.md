# Third-Party License Notes

Project license: MIT (`LICENSE`).

This file is a practical compatibility checklist, not legal advice.

## Runtime dependencies

| Package | Declared license | Source |
|---|---|---|
| lxml | BSD-3-Clause | https://pypi.org/project/lxml/ |
| beautifulsoup4 | MIT | https://pypi.org/project/beautifulsoup4/ |
| msal | MIT | https://pypi.org/project/msal/ |
| requests | Apache-2.0 | https://pypi.org/project/requests/ |
| tqdm | MPL-2.0 AND MIT | https://pypi.org/project/tqdm/ |

## Build tool

| Tool | Declared license | Source |
|---|---|---|
| PyInstaller | GPL-2.0 with exception (plus Apache-2.0 for some files) | https://pyinstaller.org/en/stable/license.html |

PyInstaller's exception states generated executables can be shipped under your own license, while respecting licenses of bundled dependencies.

## Reference-only projects (not dependencies)

| Project | License | Usage in this repo |
|---|---|---|
| ENML_PY | MIT | Referenced for ideas/docs only; no code import or vendored files |

Sources:
- https://github.com/CarlLee/ENML_PY
