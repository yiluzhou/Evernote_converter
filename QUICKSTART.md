# Quick Start

## Pick Your Path

| User Type | Effort | Difficulty | First Action |
|----------|--------|------------|--------------|
| General User | ~5-10 min | ★☆☆☆☆ | Double-click `Build_EvernoteToOneNoteWizard.bat` |
| Advanced User (Pro) | ~10-20 min | ★★★☆☆ | Create env + run `scripts\\build_windows_exe.bat` |
| Developer | ~10 min | ★★★★☆ | Run `python src/main.py` |

## General Users (Recommended)

1. Open this GitHub repository page.
2. Click **Code** -> **Download ZIP**.
3. In Downloads, right-click the ZIP -> **Extract All...**.
4. Open the extracted folder.
5. In the project main folder, double-click `Build_EvernoteToOneNoteWizard.bat`.
6. Run `EvernoteToOneNoteWizard.exe` in the same main folder after build completes.

Notes:
- No installer/uninstaller needed.
- If Python is missing, the script auto-downloads and installs local Python 3.14 into `.python_runtime`.
- Build dependencies are installed only into local `.build_env`.
- Existing system Python/conda environments are not modified by the general-user script.
- Internet connection is required for first-time auto bootstrap.

## Advanced Users (Pro): Build `.exe`

Use one environment option, then run:

```bat
Build_EvernoteToOneNoteWizard_Advanced.bat
```

### Option A: conda

```bash
conda create -n evernote python=3.14 -y
conda activate evernote
pip install -r requirements.txt pyinstaller
```

### Option B: venv

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt pyinstaller
```

### Option C: uv

```bash
uv venv --python 3.14
.venv\Scripts\activate
uv pip install -r requirements.txt pyinstaller
```

Output:
- `EvernoteToOneNoteWizard.exe` in the project main folder

## Developer Path

Set up a Python environment first.

### Option A: venv

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python src/main.py
```

### Option B: conda

```bash
conda create -n evernote-dev python=3.14 -y
conda activate evernote-dev
pip install -r requirements.txt
python src/main.py
```

### Option C: uv

```bash
uv venv --python 3.14
.venv\Scripts\activate
uv pip install -r requirements.txt
python src/main.py
```

## Azure Setup (One-Time)

Required in Azure app registration:
- Personal Microsoft accounts only
- Delegated permissions: `Notes.Create`, `Notes.ReadWrite`
- `Allow public client flows = Yes`

The GUI has full setup instructions and now gives explicit failure reasons with a copyable ChatGPT prompt if setup is wrong.

## Duplicate Handling

- No automatic notebook/section deletion.
- Duplicate check uses Evernote GUID first, then title+created fallback.
- For duplicates, you can choose: `replace`, `replace all`, `skip`, `skip all`.
