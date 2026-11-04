@echo off
rem Run manage.py with the project venv interpreter.
"%~dp0.venv\Scripts\python.exe" "%~dp0manage.py" %*
