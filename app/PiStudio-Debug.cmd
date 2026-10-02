@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title PI Studio Debug

rem ============================================================
rem  PI Studio - debug / diagnostics launcher
rem  Prints a full self-test, then starts either the web server
rem  (no args) or whatever you pass (e.g. --ask "hello").
rem ============================================================

set "PYTHONUTF8=1"
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

if not defined PY ( echo [PI Studio] Python 3.8+ not found. ^(bundled: ..\runtime\python\python.exe^) & pause & exit /b 1 )

echo ================================================================
echo  PI Studio Debug - this window shows all output and errors
echo  Interpreter: %PY%
echo  Args: --serve / --selftest / --probe / --ask "question" / --chat
echo ================================================================
echo.
"%PY%" pistudio.py --selftest
echo.
echo ---- self-test done ----
echo.
if "%~1"=="" ( "%PY%" pistudio.py --serve ) else ( "%PY%" pistudio.py %* )
echo.
echo [exit code %errorlevel%]
pause
