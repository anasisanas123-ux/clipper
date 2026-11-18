@echo off
rem Double-click to install or update Clipper (needs internet; Windows may ask permission while tools install).
rem This one file is enough: it always fetches the latest setup script, and only uses the copy next to it
rem when there's no internet.
set "PS1=%TEMP%\clipper-install.ps1"
del "%PS1%" 2>nul
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Headers @{'Cache-Control'='no-cache'} ('https://raw.githubusercontent.com/anasisanas123-ux/clipper/main/install.ps1?t=' + [DateTime]::Now.Ticks) -OutFile $env:TEMP\clipper-install.ps1 } catch { }"
if not exist "%PS1%" set "PS1=%~dp0install.ps1"
if not exist "%PS1%" (
  echo Couldn't download the setup script. Check the internet connection and try again.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 (
  echo.
  echo Setup stopped because of the error above.
)
pause
