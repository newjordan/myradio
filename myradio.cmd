@echo off
rem Local checkout launcher for Windows. No network, administrator rights or model download.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  rem "python" may be the Microsoft Store placeholder, which cannot run anything: try it before trusting it.
  where python >nul 2>nul && python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo myradio needs Python 3.10 or later. Install it from https://www.python.org/downloads/windows/
  echo ^(tick "Add python.exe to PATH" in the installer^), then open this launcher again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))"
if errorlevel 1 (
  echo Please upgrade to Python 3.10 or later: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
%PY% -m myradio.server %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
