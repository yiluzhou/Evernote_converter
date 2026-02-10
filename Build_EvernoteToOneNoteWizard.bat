@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo EvernoteToOneNoteWizard Builder
echo ============================================================
echo This script creates a local Python environment and builds:
echo   EvernoteToOneNoteWizard.exe
echo Safety: this script uses local folders only and does not modify your conda envs.
echo.

set "PYTHON_VERSION=3.14.0"
set "LOCAL_PY_DIR=%CD%\.python_runtime"
set "LOCAL_PY=%LOCAL_PY_DIR%\python.exe"
set "PY_CMD="
set "IS_LOCAL_PY=0"

if exist "%LOCAL_PY%" (
  set "PY_CMD=%LOCAL_PY%"
  set "IS_LOCAL_PY=1"
)

if not defined PY_CMD (
  for /f "usebackq delims=" %%P in (`py -3.14 -c "import sys; print(sys.executable)" 2^>nul`) do set "PY_CMD=%%P"
)

if not defined PY_CMD (
  for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do set "PY_CMD=%%P"
)

if not defined PY_CMD (
  for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do set "PY_CMD=%%P"
)

if not defined PY_CMD (
  echo Python not found. Bootstrapping local Python %PYTHON_VERSION%...
  call :BOOTSTRAP_LOCAL_PYTHON
  if errorlevel 1 (
    echo Failed to bootstrap local Python.
    pause
    exit /b 1
  )
  set "PY_CMD=%LOCAL_PY%"
  set "IS_LOCAL_PY=1"
)

if "%IS_LOCAL_PY%"=="1" (
  call "%PY_CMD%" -c "import tkinter" >nul 2>&1
  if errorlevel 1 (
    echo Local Python is missing Tk support. Repairing local Python...
    call :BOOTSTRAP_LOCAL_PYTHON
    if errorlevel 1 (
      echo Failed to repair local Python with Tk support.
      pause
      exit /b 1
    )
  )
)

set "ENV_DIR=.build_env"
echo Using interpreter: %PY_CMD%
echo Creating/updating local environment: %ENV_DIR%
call "%PY_CMD%" -m venv "%ENV_DIR%"
if errorlevel 1 (
  if "%IS_LOCAL_PY%"=="0" (
    echo Existing Python could not create venv. Falling back to local Python %PYTHON_VERSION%...
    call :BOOTSTRAP_LOCAL_PYTHON
    if errorlevel 1 (
      echo Failed to bootstrap local Python.
      pause
      exit /b 1
    )
    set "PY_CMD=%LOCAL_PY%"
    set "IS_LOCAL_PY=1"
    call "%PY_CMD%" -m venv "%ENV_DIR%"
  )
)
if errorlevel 1 (
  echo Failed to create local environment.
  pause
  exit /b 1
)

set "ENV_PY=%ENV_DIR%\Scripts\python.exe"
if not exist "%ENV_PY%" (
  echo Missing Python executable in %ENV_DIR%.
  pause
  exit /b 1
)

call scripts\build_windows_exe.bat "%ENV_PY%"
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Build succeeded.
echo You can now run:
echo   EvernoteToOneNoteWizard.exe
echo.
pause
endlocal
exit /b 0

:BOOTSTRAP_LOCAL_PYTHON
set "PY_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-amd64.exe"
set "INSTALLER=%TEMP%\python-%PYTHON_VERSION%-amd64.exe"

if exist "%LOCAL_PY_DIR%" (
  echo Reusing existing local Python folder: %LOCAL_PY_DIR%
) else (
  mkdir "%LOCAL_PY_DIR%" >nul 2>&1
)

echo Downloading Python installer...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%INSTALLER%'"
if errorlevel 1 (
  echo Download failed: %PY_URL%
  exit /b 1
)

echo Installing Python into project-local folder...
start /wait "" "%INSTALLER%" /quiet InstallAllUsers=0 Include_launcher=0 Include_pip=1 PrependPath=0 Include_test=0 Include_tcltk=1 Include_dev=0 Include_debug=0 Include_symbols=0 Shortcuts=0 AssociateFiles=0 TargetDir="%LOCAL_PY_DIR%"
if errorlevel 1 (
  echo Python installer failed.
  exit /b 1
)

if not exist "%LOCAL_PY%" (
  echo Local Python executable not found after install:
  echo   %LOCAL_PY%
  exit /b 1
)

echo Local Python installed: %LOCAL_PY%
exit /b 0
