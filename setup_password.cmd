@echo off
rem ---------------------------------------------------------------
rem Double-click this file to:
rem   1) set the Windows login password for this account, and
rem   2) store the SAME password into the voice-unlock credential store
rem
rem Why one program does both:
rem   Entering the password in two separate places invites a mismatch, and
rem   the only symptom is "voiceprint passed but logon failed" -- very hard
rem   to diagnose. Doing it in one place removes that failure mode.
rem
rem The password is read with getpass (no echo) only inside THIS window.
rem It is never written to a log and never leaves your session in plaintext.
rem
rem IMPORTANT: keep this file pure ASCII. cmd.exe parses .bat/.cmd files
rem with the ANSI codepage (cp936 here), so UTF-8 Chinese bytes get
rem mis-decoded, can swallow the following newline, and the actual python
rem command line silently never runs. That bug cost real time -- see the
rem design doc. Chinese UI text is printed by the python script instead.
rem ---------------------------------------------------------------
cd /d %~dp0
title VoiceUnlock - Set Login Password
echo ============================================================
echo  VoiceUnlock - set Windows password + store credential
echo ============================================================
echo.
echo  This window will do two things with the password you type:
echo    1) set it as this machine's login password
echo    2) store it (DPAPI encrypted) for voiceprint unlock
echo.
echo  Why both at once: if the two places ever disagree, the symptom is
echo  "voiceprint passed but logon failed" -- painful to diagnose.
echo.
echo ------------------------------------------------------------
venv\Scripts\python.exe -u tools\setup_password.py
set RC=%ERRORLEVEL%
echo ------------------------------------------------------------
echo.
echo  exit code = %RC%
echo  (0 = credential works, 2 = cancelled, 1 = something failed)
echo.
echo  Window is kept open so you can read the result above.
pause >nul
