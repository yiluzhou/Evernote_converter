@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo EvernoteToOneNoteWizard Advanced Builder
echo ============================================================
echo This script builds EvernoteToOneNoteWizard.exe in this folder.
echo It will only use an isolated Python environment.
echo.

set "PY=%~1"

if not defined PY (
  if defined VIRTUAL_ENV (
    echo Detected active virtual environment:
    echo   %VIRTUAL_ENV%
    set "PY=python"
  ) else (
    if defined CONDA_PREFIX (
      echo Detected active conda environment:
      echo   %CONDA_PREFIX%
      set "PY=python"
    ) else (
      echo No active isolated environment detected.
      echo.
      echo Paste the full path to python.exe inside your env.
      echo Example:
      echo   C:\path\to\project\.venv\Scripts\python.exe
      set /p PY=Env python.exe path - blank to cancel: 
      if not defined PY (
        echo Cancelled.
        pause
        exit /b 1
      )
    )
  )
)

echo.
echo Building with:
echo   %PY%
call scripts\build_windows_exe.bat "%PY%"
if not "!ERRORLEVEL!"=="0" (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Build succeeded:
echo   EvernoteToOneNoteWizard.exe
pause
endlocal
