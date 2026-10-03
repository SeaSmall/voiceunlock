@echo off
rem ---------------------------------------------------------------
rem Re-save the credential WITHOUT touching the Windows password.
rem Run this when the password is already set correctly but the
rem voiceprint agent cannot read credential.bin any more.
rem
rem Why that happens:
rem   credential.bin is protected with DPAPI *user scope*. That only
rem   works if the process saving it and the process reading it share
rem   the same logon session's master key. If the account password was
rem   changed *after* this logon session was created, the session's
rem   cached key no longer unlocks the re-protected master key, and
rem   reads fail with 0x8009000B (NTE_BAD_KEY_STATE).
rem   Re-saving inside the CURRENT session fixes it for that session.
rem
rem Pure ASCII on purpose (cmd.exe parses .cmd with the ANSI codepage).
rem ---------------------------------------------------------------
cd /d %~dp0
title VoiceUnlock - Re-save Credential
echo ============================================================
echo  VoiceUnlock - re-save credential (password NOT changed)
echo ============================================================
echo.
echo  Type the SAME password you just set for Windows.
echo  Nothing about the account is modified here.
echo.
echo ------------------------------------------------------------
venv\Scripts\python.exe -u tools\setup_password.py --keep-password
set RC=%ERRORLEVEL%
echo ------------------------------------------------------------
echo.
echo  exit code = %RC%   (0 = credential works)
echo.
echo  Window is kept open so you can read the result above.
pause >nul
