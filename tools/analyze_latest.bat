@echo off
rem Double-click: analyze the newest run in logs\ and open the plot.
cd /d "%~dp0.."
python tools\analyze_run.py %*
for /f "delims=" %%f in ('dir /b /o-d logs\*_trimmed.png 2^>nul') do (start "" "logs\%%f" & goto done)
:done
pause
