@echo off
REM ---------------------------------------------------------------------------
REM run.bat -- daily refresh: company master, revenue, then the dashboard.
REM
REM   run.bat              daily   (master + last 3 months + current-month APIs)
REM   run.bat backfill     full    (master + every month from BACKFILL_START)
REM
REM The master is refreshed on every run. It is a single ~1,983-row API call, and
REM the alternative -- a newly listed company showing up with no Korean or
REM English name until someone remembers to run it -- is worse than the cost.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set PY=python
where %PY% >nul 2>nul || (echo [ERROR] python not found on PATH & exit /b 1)

echo ============================================================
echo [1/4] company master
echo ============================================================
%PY% fetch.py --master
if errorlevel 1 (echo [ERROR] master fetch failed & exit /b 1)

echo.
echo ============================================================
if /i "%~1"=="backfill" (
  echo [2/4] revenue -- FULL BACKFILL
  echo ============================================================
  %PY% fetch.py --backfill
) else (
  echo [2/4] revenue -- daily refresh
  echo ============================================================
  %PY% fetch.py
)
if errorlevel 1 (echo [ERROR] revenue fetch failed & exit /b 1)

echo.
echo ============================================================
echo [3/4] Korean readings ^(names_auto.py^)
echo ============================================================
REM exits 1 when a hanja is unmapped -- that is a warning, not a failure,
REM so the build still runs and the unmapped characters get reported.
%PY% gen_names_auto.py
if errorlevel 1 echo [WARN] unmapped hanja above -- add them to hanja_kr.py

echo.
echo ============================================================
echo [4/4] dashboard.html
echo ============================================================
REM --modal-all ships the detail-modal series for all ~949 rows, not just the
REM 113 BoM stocks, which makes every table row clickable (+620 KB).
%PY% build.py --modal-all
if errorlevel 1 (echo [ERROR] build failed & exit /b 1)

echo.
echo done. opening dashboard...
start "" "%~dp0dashboard.html"
endlocal
