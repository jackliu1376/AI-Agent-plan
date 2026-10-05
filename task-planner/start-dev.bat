@echo off
rem ============================================================
rem  Cairn dev launcher -- starts BOTH:
rem    - API  (FastAPI)  on 127.0.0.1:8000
rem    - Web  (Vite)     on localhost:5173
rem  Ctrl+C stops both. All output text lives in scripts/dev.mjs
rem  so this file stays ASCII-only (no encoding surprises in cmd).
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0frontend"
call npm run dev:all
if errorlevel 1 pause
