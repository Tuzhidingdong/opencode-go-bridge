@echo off
setlocal
set "LOGDIR=%APPDATA%\AntigravityTokenMeter"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
>>"%LOGDIR%\dispatcher_probe.log" echo %DATE% %TIME% event=%~1

if /I "%~1"=="SessionStart" (
    echo {"injectSteps":[]}
) else if /I "%~1"=="PreInvocation" (
    echo {"injectSteps":[]}
) else if /I "%~1"=="PostInvocation" (
    echo {"injectSteps":[],"terminationBehavior":""}
) else if /I "%~1"=="Stop" (
    echo {"decision":"stop"}
) else (
    echo {}
)
exit /b 0