@echo off
setlocal
set "PYTHONIOENCODING=utf-8"
"D:\Software\Anacoda\python.exe" "D:\AI_Tools\opencode-go-bridge\antigravity_opencode_project_router.py"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
