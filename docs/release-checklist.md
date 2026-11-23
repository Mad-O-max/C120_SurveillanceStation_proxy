# Release checklist

## Local development

- [x] Run the v3.1 diagnostic baseline and preserve its protocol regression tests.
- [x] Run v3.2 protocol tests with CPython 3.8.
- [x] Run POSIX credential, start, duplicate start, status, crash recovery, restart and graceful-stop tests with CPython 3.8 on Linux.
- [x] Review repository contents for local credentials, captures and vendor files.
- [x] Add the MIT license selected by the project owner.
- [ ] Publish or push after separate owner authorization.

Run `python3 c120_onvif_proxy.py --self-test` and `python3 -m unittest discover -s tests -v` from the project root. Run the POSIX tests on a local filesystem under a regular user.

## DS212+ acceptance

| Check | Status |
| --- | --- |
| Boot task starts unattended after NAS reboot | Passed on the DS212+ with v3.2.0, owner-reported. |
| 24 hours with multiple renewals and recorded CPU and memory usage | Not run |
| Detection before and after Renew in Tapo, DS Cam and archive | Limited to predecessor evidence |
| Camera power cycle and network interruption | Not run with v3.2 |
| Stop, restart and duplicate handling | Run locally on Linux. NAS pending. |
| Logging remains bounded and free of secrets over 24 hours | Rotation and redaction tested locally. NAS pending. |
| RTSP recordings remain usable | Limited to predecessor evidence and local binary relay tests. Investigate repeated resets with a short opt-in RTSP capture. |
| Installation, uninstall and rollback | Installation passed on the DS212+ with v3.2.0, owner-reported. Uninstall and rollback pending. |
| Isolated event mapping and active profile trial | Not run |
| Local syslog receipt and route | Not run. Deployment uses rotating log files. |

Record the DSM, package, firmware and Python versions, timestamps and results for each hardware check. Keep raw captures and logs private.
