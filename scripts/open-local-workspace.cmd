@echo off
setlocal
rem Dedicated local-workspace browser profile: leaves corporate proxy settings intact.
set "WORKSPACE_EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%WORKSPACE_EDGE%" set "WORKSPACE_EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%WORKSPACE_EDGE%" set "WORKSPACE_EDGE=%LocalAppData%\Microsoft\Edge\Application\msedge.exe"
if not exist "%WORKSPACE_EDGE%" (
    echo Microsoft Edge was not found.
    pause
    exit /b 1
)
start "" "%WORKSPACE_EDGE%" --user-data-dir="%LOCALAPPDATA%\CamstarWorkspace\Browser" --no-proxy-server --no-first-run --app=http://127.0.0.1:8030/
