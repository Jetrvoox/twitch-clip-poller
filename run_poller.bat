@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=C:\Users\Jethr\AppData\Local\Programs\Python\Python312\python.exe"
set "LOG_FILE=%PROJECT_DIR%poller_task.log"

cd /d "%PROJECT_DIR%"

echo ==== %date% %time% ==== >> "%LOG_FILE%"
"%PYTHON_EXE%" "%PROJECT_DIR%poller.py" 497416 >> "%LOG_FILE%" 2>&1
echo. >> "%LOG_FILE%"

endlocal
