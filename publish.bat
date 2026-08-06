@echo off
REM ---------------------------------------------------------------------------
REM publish.bat -- deploy dashboard.html to the gh-pages branch.
REM
REM NOTE: this file must stay ASCII-only. cmd.exe reads .bat files in the system
REM OEM codepage, not UTF-8, so non-ASCII comments get parsed as commands.
REM
REM The point: never accumulate commits.
REM   The dashboard is a single 2.5MB HTML that changes completely every day.
REM   git cannot delta-compress that, so committing daily would leave
REM   2.5MB x 365 = ~900MB permanently in the repo. Instead we build a brand new
REM   repo in a temp folder with exactly one commit and force-push it, so the
REM   remote gh-pages branch always holds precisely one commit.
REM
REM Working in a temp folder also keeps the main repo's index and working tree
REM untouched -- checking out an orphan branch in place would make every file in
REM the project folder vanish and reappear.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "dashboard.html" (
  echo [publish] dashboard.html not found -- run build first.
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
git commit -q -m "dashboard %DATE% %TIME%" || (popd & exit /b 1)
git remote add origin "%ORIGIN%" || (popd & exit /b 1)
git push -q --force origin gh-pages
set RC=%ERRORLEVEL%
popd

rmdir /s /q "%STAGE%" 2>nul
if not "%RC%"=="0" (
  echo [publish] push failed with exit %RC%
  exit /b %RC%
)
echo [publish] deployed OK
exit /b 0
