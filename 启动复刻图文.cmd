@echo off
setlocal
cd /d "%~dp0runtime\app"
"%~dp0runtime\python\python.exe" "%~dp0runtime\app\run_engine.py" %*
if errorlevel 1 pause
