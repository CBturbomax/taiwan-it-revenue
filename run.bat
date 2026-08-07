@echo off
REM ---------------------------------------------------------------------------
REM run.bat -- ask the GitHub server to rebuild the dashboard. That is all.
REM
REM This machine blue-screens under heavy file I/O (bugcheck 0x50 in a stacked
REM filesystem-filter driver, five times since 2026-06, security software that
REM cannot be removed). So this script does NOT build anything locally:
REM no SQLite, no HTML, no git. One HTTPS call and it is done.
REM
REM Side benefit: the server becomes the only writer, so the disclosure
REM (first_seen) timestamps all come from one clock instead of two.
REM
REM For a local build - only when the server itself is broken - see
REM build-local.bat, and read the warning at the top of it first.
REM
REM NOTE: this file must stay ASCII-only. cmd.exe reads .bat files in the system
REM OEM codepage, not UTF-8, so non-ASCII text gets parsed as commands.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set REPO=CBturbomax/taiwan-it-revenue
set SITE=https://cbturbomax.github.io/taiwan-it-revenue/
set RUNS=https://github.com/%REPO%/actions

REM cheap guard: warn if any batch file here has non-ASCII bytes, which is what
REM produces the garbled "'???' is not recognized as an internal command" errors
where python >nul 2>nul && (
  python "%~dp0check_ascii.py" >nul 2>nul
  if errorlevel 1 (
    echo [WARN] a .bat/.cmd file here contains non-ASCII characters.
    echo [WARN] run:  python check_ascii.py
    echo.
  )
)

set GH=gh
where gh >nul 2>nul || set GH="C:\Program Files\GitHub CLI\gh.exe"

%GH% workflow run "update dashboard" --repo %REPO%
if errorlevel 1 (
  echo.
  echo [ERROR] Could not reach GitHub.
  echo         Check the network, or run:  gh auth status
  echo         You can also start it by hand at:
  echo         %RUNS%
  echo.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  Requested a refresh on the server.
echo  Nothing was built on this PC.
echo.
echo  Wait about 2 minutes, then reload the page:
echo    %SITE%
echo.
echo  Progress:
echo    %RUNS%
echo ============================================================
echo.
endlocal
exit /b 0
