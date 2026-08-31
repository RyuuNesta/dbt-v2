@echo off
REM ===========================================================================
REM  ASG dbt Studio launcher
REM
REM  Double-click this file. It finds the Python that has dbt installed, checks
REM  the environment, then starts the UI and opens a browser.
REM ===========================================================================

setlocal
cd /d "%~dp0.."

echo.
echo   ASG dbt Studio
echo   ----------------------------------------------------------------
echo.

REM --- locate an interpreter that can import dbt ---------------------------
set "PYEXE="

for %%P in (python.exe py.exe) do (
    if not defined PYEXE (
        %%P -c "import dbt.version" >nul 2>&1
        if not errorlevel 1 set "PYEXE=%%P"
    )
)

if not defined PYEXE (
    echo   [X] Could not find a Python with dbt installed.
    echo.
    echo       Tried: python, py
    echo.
    echo       If dbt lives in a virtual environment, activate it first:
    echo           .venv\Scripts\activate
    echo           python dbt_ui\serve.py
    echo.
    pause
    exit /b 1
)

echo   Using interpreter: %PYEXE%
echo.

REM --- pre-flight ----------------------------------------------------------
%PYEXE% dbt_ui\serve.py --check
if errorlevel 1 (
    echo.
    echo   [X] The environment check found problems. Fix them and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting the server. Close this window or press Ctrl+C to stop.
echo.

%PYEXE% dbt_ui\serve.py %*

echo.
echo   Server stopped.
pause
endlocal
