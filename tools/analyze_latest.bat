@echo off
rem Double-click: analyze the newest run in logs\ (any revision folder) and open the plot.
cd /d "%~dp0.."
python tools\analyze_run.py %*
powershell -NoProfile -Command "$f = Get-ChildItem logs -Recurse -Filter *_trimmed.png | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if ($f) { Invoke-Item $f.FullName }"
pause
