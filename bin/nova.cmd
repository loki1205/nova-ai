@echo off
setlocal
set "NOVA_HOME=%~dp0.."
"%NOVA_HOME%\.venv\Scripts\python.exe" "%NOVA_HOME%\nova.py" %*
