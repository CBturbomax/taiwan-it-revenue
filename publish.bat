@echo off
REM ===========================================================================
REM  publish.bat -- deploy dashboard.html to gh-pages.
REM
REM  DEPLOYMENT IS THE SERVER'S JOB NOW. Use run.bat, which asks GitHub
REM  Actions to rebuild and publish. This script exists for the case where
REM  the server itself is broken.
REM
REM  Why it is guarded: this script only COPIES dashboard.html -- it never
REM  builds it. Run it on its own and it happily publishes whatever stale
REM  file is lying around, silently replacing a fresh server build with an
REM  older one. That already happened once: a local build at 09:23 pushed a
REM  dashboard that predated the timeline feature.
REM
REM  Two locks, because an age-based check was not enough -- a 2.5-hour-old
REM  local build still overwrote a 20-minute-old server build:
REM    1. it does nothing at all without --force
REM    2. even with --force it compares the local build time against what is
REM       LIVE right now, and refuses if the deployed one is newer
REM
REM  NOTE: this file must stay ASCII-only. cmd.exe reads .bat files in the
REM  system OEM codepage, not UTF-8, so non-ASCII text is parsed as commands.
REM ===========================================================================
setlocal
cd /d "%~dp0"

if /i not "%~1"=="--force" (
  echo.
  echo ============================================================
  echo  publish.bat is disabled for routine use.
  echo.
  echo  Deployment belongs to the GitHub server now. This script
  echo  only COPIES dashboard.html, so running it by hand pushes
  echo  whatever old file is sitting here and silently replaces a
  echo  newer server build. That has already happened twice.
  echo.
  echo  To update the site:   run.bat
  echo  To override anyway:   publish.bat --force
  echo ============================================================
  echo.
  exit /b 1
)

if not exist "dashboard.html" (
  echo [publish] dashboard.html not found -- nothing to deploy.
  echo [publish] The server builds and publishes on its own; use run.bat.
  exit /b 1
)

where python >nul 2>nul || (
  echo [publish] python not found, cannot compare against the live site.
  exit /b 1
)
python "%~dp0check_fresh.py" "%~dp0dashboard.html"
if errorlevel 1 (
  echo.
  echo [publish] REFUSING -- the deployed dashboard is newer than this one.
  echo [publish] Use run.bat to have the server rebuild.
  echo.
  exit /b 1
)

set ORIGIN=
for /f "delims=" %%i in ('git config --get remote.origin.url 2^>nul') do set ORIGIN=%%i
if "%ORIGIN%"=="" (
  echo [publish] no 'origin' remote configured.
  exit /b 1
)

set STAGE=%TEMP%\ghpages-twrev
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%" || exit /b 1

REM publish as index.html so the bare repo URL opens the dashboard
copy /y "dashboard.html" "%STAGE%\index.html" >nul || exit /b 1
REM .nojekyll skips Jekyll preprocessing: faster builds, no filename rules
break > "%STAGE%\.nojekyll"

pushd "%STAGE%"
git init -q -b gh-pages || (popd & exit /b 1)
git config user.name "CBturbomax"
git config user.email "cbpark@wisdomasset.co.kr"
git add -A || (popd & exit /b 1)
git commit -q -m "dashboard (local) %DATE% %TIME%" || (popd & exit /b 1)
git remote add origin "%ORIGIN%" || (popd & exit /b 1)
git push -q --force origin gh-pages
set RC=%ERRORLEVEL%
popd

rmdir /s /q "%STAGE%" 2>nul
if not "%RC%"=="0" (
  echo [publish] push failed with exit %RC%
  exit /b %RC%
)
echo [publish] deployed OK ^(local build^)
exit /b 0
