@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title PI Studio

rem ============================================================
rem  PI Studio - native desktop app launcher
rem  Double-click this file to open the Tkinter desktop window.
rem  For the web version use PiStudio-Web.cmd
rem  For diagnostics use PiStudio-Debug.cmd
rem ============================================================

for %%V in (PYTHONUTF8) do set "%%V=1"

set "PY="

rem ---- 1) bundled portable Python (runtime/python) - prefer this ----
if exist "%~dp0..\runtime\python\pythonw.exe" (
  "%~dp0..\runtime\python\pythonw.exe" -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul
  if !errorlevel! equ 0 set "PY=%~dp0..\runtime\python\pythonw.exe"
)

rem ---- 2) fallback: python on PATH ----
for %%C in (pythonw.exe python.exe) do (
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
  echo  [PI Studio] Python 3.8+ with tkinter was not found.
  echo  Bundled runtime missing? Expected: ..\runtime\python\pythonw.exe
  echo  Or install the official Python: https://www.python.org/downloads/
  echo  ^(check "Add python.exe to PATH" during setup^)
  echo.
  echo  Tried:
  where python 2>nul
  where pythonw 2>nul
  echo.
  pause
  exit /b 1
)

start "" "%PY%" "%~dp0pistudio.py"
exit /b 0
