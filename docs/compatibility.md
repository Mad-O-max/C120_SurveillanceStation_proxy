# Compatibility and evidence

| Component | v3.1 hardware evidence |
| --- | --- |
| NAS | Synology DS212+ |
| DSM | 6.2.4-25556 Update 8 |
| Kernel | 2.6.32.12 |
| Surveillance Station | 9.2.0-9289 |
| Device Pack | 6.2.1-5482 |
| Camera | TP-Link Tapo C120 |
| Firmware | 1.9.4 Build 260813 |
| NAS Python | 3.8.6 |
| Network | Camera ONVIF TCP 2020, subscription TCP 1024, RTSP TCP 554. Proxy loopback TCP 12020 and 12554. |

The v3.1 hardware report recorded two successful renewals and 52 forwarded PeopleDetector notifications. Four test passages reached Tapo and DS Cam. A separate test confirmed archive markers. Video and recordings worked in the test, with one unattributed RTSP reset around startup.

The proxy targets Python 3.8.6 to support the older DS212+ on DSM 6.2.4. This is the newest Python version listed for that combination in Synology's official Python3 package (build `3.8.6-0154`). [Synology Python3 package for DSM 6.2](https://www.synology.com/en-global/dsm/packages/py3k?os_ver=6.2)

The v3.2 line has local tests on Windows with CPython 3.8.20 and Ubuntu under WSL with CPython 3.8.10. The local checks exercise routing, fresh WSSE, two simulated renewal cycles, recovery and resume, binary RTSP, credential permissions, log rotation and service control. The owner confirmed installation and unattended startup after a DS212+ reboot with v3.2.0.

In a November 23–25, 2026 run on the DS212+, v3.2.0 logged 254 successful subscription renewals across about 40 hours and 36 minutes before the first error associated with a deliberate camera power cut. The logs show brief RTSP client disconnects earlier in the run. Following the power cut, RTSP reconnected after about 64 seconds and the ONVIF subscription recovered after about 89 seconds, both measured from the first logged error. The proxy stayed running. CPU and memory usage, recording playback and alerts after recovery require separate evidence.

Other limited-update platforms may benefit from the workaround. Additional versions and community results remain unverified.
