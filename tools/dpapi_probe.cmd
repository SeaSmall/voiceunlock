@echo off
rem Pure ASCII on purpose: cmd.exe parses .cmd with the ANSI codepage,
rem and UTF-8 Chinese bytes will silently swallow the next line.
cd /d "%~dp0.."
venv\Scripts\python.exe tools\dpapi_probe.py --tag console > logs\dpapi_probe_console.log 2>&1
