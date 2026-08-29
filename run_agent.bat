@echo off
title Kangaroo Options Agent - paper PA310P0ROWX0
cd /d C:\Users\HMz\Documents\Source\KangarooOptions
if not exist logs mkdir logs
echo === agent launch === >> "C:\Users\HMz\Documents\Source\KangarooOptions\logs\agent.log"
"C:\Users\HMz\AppData\Local\Programs\Python\Python313\python.exe" -u agent.py 2>&1 | powershell -NoProfile -Command "$sw=[IO.StreamWriter]::new('C:\Users\HMz\Documents\Source\KangarooOptions\logs\agent.log',$true,[Text.Encoding]::UTF8);try{$input|ForEach-Object{Write-Host $_;$sw.WriteLine($_);$sw.Flush()}}finally{$sw.Close()}"
echo EXIT_CODE=%ERRORLEVEL% >> "C:\Users\HMz\Documents\Source\KangarooOptions\logs\agent.log"
echo Agent beendet - Konsole bleibt offen (EXIT_CODE siehe logs\agent.log)
pause
