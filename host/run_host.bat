@echo off
rem Entry point launched by Chrome. Native messaging manifests cannot pass
rem arguments, so this batch file supplies the script path.
rem ASCII only: cmd.exe reads .bat in the ANSI codepage and mangles UTF-8.
rem -u keeps stdio unbuffered so progress is not delayed.
python -u "%~dp0native_host.py"
