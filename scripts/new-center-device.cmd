@echo off
setlocal EnableExtensions

rem One entry point for a center's device. The mode says which program starts:
rem   offline : the printer agent (prints the batch and logs each copy on the blockchain)
rem   online  : the exam server (candidate screens, answer sheets, Merkle root on the blockchain)
rem The mode the BOARD chose at generation is written on the blockchain; each program checks it and refuses
rem to run the wrong kind of exam, so this argument cannot override the board.
rem Usage: scripts\new-center-device.cmd center_013 offline
rem        scripts\new-center-device.cmd center_013 online

if "%~2"=="" goto usage
if /i "%~2"=="offline" goto offline
if /i "%~2"=="online" goto online

:usage
echo Usage: scripts\new-center-device.cmd center_013 offline^|online
exit /b 2

:offline
call "%~dp0new-printer.cmd" %~1
exit /b %errorlevel%

:online
call "%~dp0new-exam-center.cmd" %~1 %~3
exit /b %errorlevel%
