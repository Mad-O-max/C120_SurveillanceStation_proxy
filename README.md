# Tapo C120 ONVIF proxy for Synology Surveillance Station

This workaround lets Surveillance Station use the Tapo C120's camera-side person detection for motion events while recording over RTSP/TCP. In the documented setup, person events reached DS Cam and appeared as recording markers. The proxy forwards the camera's PeopleDetector notifications. Surveillance Station classifies them as motion through a separate event mapping.

## Quick start

1. Check the [compatibility matrix](docs/compatibility.md). The end-to-end camera test used diagnostic v3.1. The owner confirmed installation and unattended boot for v3.2.0 on the DS212+. Further NAS acceptance checks are pending.
2. Install with [DSM 6 Task Scheduler](docs/synology-dsm6.md). Use Python 3.8.6 or newer and the templates in [config](config/). Replace CAMERA_IP with the camera's IPv4 address and use the Tapo camera account.
3. In Surveillance Station, use camera address 127.0.0.1 and HTTP port 12020. Under advanced device settings, select RTSP over TCP and custom RTSP port 12554. Select camera-side event detection and preserve the camera settings. Use continuous recording and configure DS Cam notifications.
4. Review the [Surveillance Station mapping](docs/event-mapping.md) in a maintenance window. Test video and a real person event before relying on the installation.

For interactive use, run `python3 c120_onvif_proxy.py --camera CAMERA_IP`. The proxy prompts for the camera account. Stop it with Ctrl+C before starting a managed instance on the same ports.

## Limits

The listeners use IPv4 loopback. Camera connections use HTTP SOAP and RTSP/TCP. Events can be lost during an outage or an interrupted PullMessages exchange. The mapping is shared by camera profiles emitting the same topic. The result comes from one older platform and firmware combination. Behavior on other releases and cameras remains unverified.

[Architecture](docs/architecture.md) | [Troubleshooting](docs/troubleshooting.md) | [Release checklist](docs/release-checklist.md)

This project is independent of Synology and TP-Link.
