@echo off
cd /d "%~dp0"
py -3 mole.py
echo.
echo [exit code: %errorlevel%]
pause
