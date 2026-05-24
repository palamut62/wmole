@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && set "PY=py -3" || set "PY=python"
%PY% -c "import importlib.util,sys;mods=['rich','send2trash','psutil'];missing=[m for m in mods if importlib.util.find_spec(m) is None];print('Missing dependencies: '+', '.join(missing)) if missing else None;sys.exit(1 if missing else 0)"
if errorlevel 1 (
  echo.
  echo Run setup.bat once to install required dependencies.
  exit /b 1
)
%PY% mole.py %*
endlocal
