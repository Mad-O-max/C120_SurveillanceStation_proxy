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

The v3.2 line has local tests on Windows with CPython 3.8.20 and Ubuntu under WSL with CPython 3.8.10. The local checks exercise routing, fresh WSSE, two simulated renewal cycles, recovery and resume, binary RTSP, credential permissions, log rotation and service control. The DS212+ deployment and exact Python 3.8.6 interpreter remain hardware acceptance work. Other limited-update platforms may benefit from the workaround. Additional versions and community results remain unverified.
