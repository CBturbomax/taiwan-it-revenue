@echo off
REM ---------------------------------------------------------------------------
REM run.bat -- daily refresh: master, revenue, Korean names, dashboard, publish.
REM
REM NOTE: this file must stay ASCII-only. cmd.exe reads .bat files in the system
REM OEM codepage, not UTF-8, so non-ASCII comments get parsed as commands.
REM
REM   run.bat              daily   (master + last 3 months + current-month APIs)
REM   run.bat backfill     full    (master + every month from BACKFILL_START)
REM
REM The master is refreshed on every run. It is a single ~1,983-row API call, and
REM the alternative -- a newly listed company showing up with no Korean or
REM English name until someone remembers to run it -- is worse than the cost.
REM
REM The last step deploys to gh-pages. If that fails (network, auth) the data and
REM the dashboard are already built, so run.bat still exits 0; the failure is
REM logged to log\publish.log and the next run retries automatically.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM ASCII only -- see the note at the top of this file.
echo ============================================================
echo  MANUAL RUN  /  su-dong sil-haeng
echo.
echo  GitHub Actions already refreshes the dashboard 4x a day.
echo  Running it here is fine when you want an update right now.
echo.
echo  CAUTION: this PC has its own data.db, so the disclosure
echo  dates (balpyoil / first_seen) recorded here will NOT match
echo  the server's. The server is the source of truth for those.
echo  Everything else - revenue, YoY, MoM - is identical.
echo ============================================================
echo.

set PY=python
where %PY% >nul 2>nul || (echo [ERROR] python not found on PATH & goto :failed)

echo ============================================================
echo [1/5] company master
echo ============================================================
%PY% fetch.py --master
if errorlevel 1 (echo [ERROR] master fetch failed & goto :failed)

echo.
echo ============================================================
if /i "%~1"=="backfill" (
  echo [2/5] revenue -- FULL BACKFILL
  echo ============================================================
  %PY% fetch.py --backfill
) else (
  echo [2/5] revenue -- daily refresh
  echo ============================================================
  %PY% fetch.py
)
if errorlevel 1 (echo [ERROR] revenue fetch failed & goto :failed)

echo.
echo ============================================================
echo [3/5] Korean readings ^(names_auto.py^)
echo ============================================================
REM exits 1 when a hanja is unmapped -- that is a warning, not a failure,
REM so the build still runs and the unmapped characters get reported.
%PY% gen_names_auto.py
if errorlevel 1 echo [WARN] unmapped hanja above -- add them to hanja_kr.py

echo.
echo ============================================================
echo [4/5] dashboard.html
echo ============================================================
REM --modal-all ships the detail-modal series for all ~949 rows, not just the
REM 113 BoM stocks, which makes every table row clickable (+620 KB).
%PY% build.py --modal-all
if errorlevel 1 (echo [ERROR] build failed & goto :failed)

echo.
echo ============================================================
echo [5/5] publish to gh-pages
echo ============================================================
if not exist "%~dp0log" mkdir "%~dp0log"
call "%~dp0publish.bat" > "%~dp0log\publish.log" 2>&1
set PUBRC=%ERRORLEVEL%
type "%~dp0log\publish.log"
if not "%PUBRC%"=="0" (
  echo [WARN] deploy failed -- data and dashboard are fine.
  echo [WARN] see log\publish.log; the next run retries automatically.
) else (
  echo   https://cbturbomax.github.io/taiwan-it-revenue/
)

echo.
echo ============================================================
echo  DONE.  Opening the dashboard...
echo  Web:  https://cbturbomax.github.io/taiwan-it-revenue/
echo ============================================================
start "" "%~dp0dashboard.html"
endlocal
REM a failed deploy is not a failed run -- always exit 0
exit /b 0

REM ---------------------------------------------------------------------------
REM On failure, hold the window open. This is normally launched by double-click,
REM and without the pause the console vanishes before the error can be read.
REM ---------------------------------------------------------------------------
:failed
echo.
echo ============================================================
echo  STOPPED - see the error above.
echo  Nothing was broken; the previous dashboard is still in place.
echo  Just run this again later, or send the message above for help.
echo ============================================================
echo.
pause
endlocal
exit /b 1
