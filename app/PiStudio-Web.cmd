@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title PI Studio Web

rem ============================================================
rem  PI Studio - local web version launcher
rem  Starts a local backend (bound to 127.0.0.1 only) and opens
rem  the browser. Close this window to stop the backend.
rem  For the native desktop app use PiStudio.cmd
rem ============================================================

for %%V in (PYTHONUTF8 PYTHONIOENCODING) do set "%%V=1"
set "PYTHONIOENCODING=utf-8"

set "PY="

rem ---- 1) bundled portable Python (runtime/python) - prefer this ----
if exist "%~dp0..\runtime\python\python.exe" (
  "%~dp0..\runtime\python\python.exe" -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul
  if !errorlevel! equ 0 set "PY=%~dp0..\runtime\python\python.exe"
)

rem ---- 2) fallback: python on PATH ----
for %%C in (python.exe python3.exe) do (
  if not defined PY (
    for /f "delims=" %%P in ('where %%C 2^>nul') do (
      if not defined PY (
        "%%P" -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul
        if !errorlevel! equ 0 set "PY=%%P"
      )
    )
  )
)

if not defined PY (
  echo.
  echo  [PI Studio] Python 3.8+ was not found.
  echo  Bundled runtime missing? Expected: ..\runtime\python\python.exe
  echo  Or install the official Python: https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

"%PY%" "%~dp0pistudio.py" --serve %*
set "RC=%errorlevel%"
echo.
echo [PI Studio] backend stopped (exit code %RC%)
pause
exit /b %RC%
