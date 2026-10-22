@echo off
rem Rosetta Patcher: double-click to open the window (needs Python 3 with numpy + pillow)
python "%~dp0rosetta_patcher.py"
if errorlevel 1 pause
