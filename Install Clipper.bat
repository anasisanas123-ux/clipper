@echo off
rem Double-click to install or update Clipper (needs internet; Windows may ask permission while tools install).
rem This one file is enough: it fetches the setup script when it isn't next to it.
set "PS1=%~dp0install.ps1"
if not exist "%PS1%" (
  set "PS1=%TEMP%\clipper-install.ps1"
  powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/anasisanas123-ux/clipper/main/install.ps1 -OutFile $env:TEMP\clipper-install.ps1"
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 (
  echo.
  echo Setup stopped because of the error above.
)
pause
