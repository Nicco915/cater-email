# Windows local HTTP connection reset

Date: 2026-11-04

Symptom: the server reports application startup complete and HTTP 200, then
`_ProactorBasePipeTransport._call_connection_lost` raises
`ConnectionResetError: [WinError 10054]` during socket shutdown.

This traceback is in the local HTTP transport cleanup path, not evidence of an
IMAP/SMTP login failure. The exact client that reset the socket cannot be
identified from this traceback alone; readiness probes and browsers are possible
clients. Check whether the server remains running and the UI can still load.

The pinned Uvicorn selects a Proactor loop by default on Windows for this
single-process configuration. CATER explicitly selects a Selector loop for the
Windows local admin server, using a Uvicorn import-string loop factory. The mail
poller continues in its own thread. No global exception handler or Python
standard-library patch suppresses errors. Non-Windows loop selection is unchanged.

Update with `git pull`, stop the existing server, and run `start-test.bat` again.
Do not rerun initialization or delete runtime data for this fix. If a traceback
continues, record the complete traceback, the installed Python/Uvicorn versions,
and whether `/api/health` still returns 200.

Regression coverage: Windows/non-Windows loop selection, Uvicorn factory import,
loop execution and default exception visibility, plus the real CLI server health
smoke test. The development machine is macOS; the original Windows Proactor reset
has not been reproduced on that platform. Windows confirmation is still required.
