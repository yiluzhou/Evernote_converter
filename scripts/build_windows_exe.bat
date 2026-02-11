@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Build standalone Windows GUI executable (single .exe in project root)
REM Run this on Windows from the project root.
REM Usage:
REM   scripts\build_windows_exe.bat [python_exe]

if "%~1"=="" (
  if defined VIRTUAL_ENV (
    echo Using active virtual environment:
    echo   %VIRTUAL_ENV%
    set "PY=python"
  ) else (
    if defined CONDA_PREFIX (
      echo Using active conda environment:
      echo   %CONDA_PREFIX%
      set "PY=python"
    ) else (
      echo ERROR: No isolated Python environment detected.
      echo.
      echo To avoid changing your system/global Python, do one of:
      echo   1 - Run Build_EvernoteToOneNoteWizard.bat
      echo   2 - Activate a venv/conda env, then run this script
      echo   3 - Pass an explicit env python:
      echo      scripts\build_windows_exe.bat .venv\Scripts\python.exe
      exit /b 1
    )
  )
) else (
  set "PY=%~1"
  echo Using explicit Python executable:
  echo   %~1
)

%PY% -c "import sys" >nul 2>&1
if not "!ERRORLEVEL!"=="0" (
  echo Python command failed: %PY%
  exit /b 1
)

%PY% -m pip install --upgrade pip
if not "!ERRORLEVEL!"=="0" (
  echo pip upgrade failed.
  exit /b 1
)
%PY% -m pip install -r requirements.txt pyinstaller
if not "!ERRORLEVEL!"=="0" (
  echo Dependency install failed.
  exit /b 1
)

%PY% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --onefile ^
  --paths src ^
  --hidden-import enex_parser ^
  --hidden-import onenote_uploader ^
  --hidden-import gui_wizard ^
  --hidden-import update_checker ^
  --distpath . ^
  --workpath build\pyinstaller ^
  --specpath build\pyinstaller ^
  --name EvernoteToOneNoteWizard ^
  src/main.py
if not "!ERRORLEVEL!"=="0" (
  echo PyInstaller build failed.
  exit /b 1
)

if not exist "EvernoteToOneNoteWizard.exe" (
  echo Build finished but EvernoteToOneNoteWizard.exe was not found in project root.
  exit /b 1
)

echo.
echo Build complete. Executable:
echo   EvernoteToOneNoteWizard.exe
endlocal
