@echo off
setlocal
if not exist "%APPDATA%\AntigravityTokenMeter" mkdir "%APPDATA%\AntigravityTokenMeter"
"D:\Software\Anacoda\python.exe" "D:\AI_Tools\opencode-go-bridge\antigravity_usage_hook.py" --session-start 2>>"%APPDATA%\AntigravityTokenMeter\launcher_stderr.log"
set "RC=%ERRORLEVEL%"
>>"%APPDATA%\AntigravityTokenMeter\launcher.log" echo event=PreInvocation exit=%RC%
exit /b %RC%