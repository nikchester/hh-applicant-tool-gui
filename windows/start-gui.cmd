@echo off
rem Opens the source GUI from a prepared checkout. A batch file, not WSH: Defender's
rem ML heuristic took the old start-gui.wsf for Trojan:Script/ObfusScript.A!ml and blanked it.
rem Keep ASCII above the error labels: Russian text is read only after "call :utf8".
setlocal DisableDelayedExpansion
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PYTHONW=%ROOT%\venvs\hh\Scripts\pythonw.exe"
set "GUI=%~dp0gui.py"
if not exist "%PYTHONW%" goto no_python
if not exist "%GUI%" goto no_gui
start "" /D "%ROOT%" "%PYTHONW%" "%GUI%" || goto launch_failed
exit /b 0

:no_python
call :utf8
echo Не найден подготовленный Python: venvs\hh\Scripts\pythonw.exe.
echo Нужно заранее подготовленное окружение этой копии проекта.
echo Загрузки и установка не выполняются.
goto failed

:no_gui
call :utf8
echo Не найден windows\gui.py. Проверь целостность папки проекта.
goto failed

:launch_failed
call :utf8
echo Не удалось запустить GUI. Проверь доступ к папке проекта и подготовленное окружение.
goto failed

:utf8
for /f "tokens=2 delims=:." %%C in ('chcp') do set "OLDCP=%%C"
chcp 65001 >nul
exit /b 0

:failed
echo.
pause
if defined OLDCP chcp %OLDCP% >nul
exit /b 1
