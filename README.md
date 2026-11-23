# Tapo C120 ONVIF proxy for Synology Surveillance Station

This local ONVIF proxy and RTSP relay lets Synology Surveillance Station use the Tapo C120's camera-side person detection for DS Cam alerts and recording markers.

## Why this project exists

The Tapo C120 streamed and recorded successfully in Surveillance Station on a Synology DS212+. Basic motion detection worked, and the Tapo app showed AI person detections. Those person detections did not trigger DS Cam alerts or appear on the Surveillance Station recording timeline.

Using the camera's built-in person detection can cut down on unwanted alerts from movement such as swaying vegetation. The NAS can keep recording while the camera handles person detection.

## What the investigation found

After checking camera settings, Synology Device Pack profiles and Surveillance Station's ONVIF event configuration, an independent ONVIF listener confirmed that the camera exposes person detections on the local network. The topic is `RuleEngine/PeopleDetector/People` and the state is carried in `IsPeople`.

- **Event recognition.** Surveillance Station needed an ONVIF event mapping that classifies PeopleDetector notifications as motion.
- **Event delivery.** TCP captures showed trouble reaching the event subscription at the address returned by the camera. The main ONVIF service used TCP port 2020, while the subscription endpoint used TCP port 1024 in the observed sessions. Subscriptions also needed timely renewal for event delivery to continue.

The main problem is that Synology Surveillance Station ignores the subscription endpoint at port 1024 provided by the camera. It is unknown if this a bug or a feature.

## How the proxy helps

The proxy routes ONVIF requests to the camera's main service or the subscription endpoint returned by the camera. It renews active subscriptions and recreates them when needed. A separate RTSP/TCP relay keeps live video and recording available when Surveillance Station connects through the loopback address.

The proxy passes through the original person detection notifications. The [Surveillance Station event mapping](docs/event-mapping.md) classifies those notifications as motion so they can trigger DS Cam alerts and appear as recording markers.

The end-to-end workaround was tested with diagnostic v3.1 on a DS212+ running DSM 6.2.4-25556 Update 8, Surveillance Station 9.2.0-9289 and Device Pack 6.2.1-5482, with a Tapo C120. Installation procedure is confirmed and unattended startup after a reboot with v3.2.0 on the same NAS. See the [compatibility matrix](docs/compatibility.md) for the documented setup and current test coverage.

## Quick start

1. **Set up a camera account in the Tapo app.** Open the C120's live view, tap the settings icon, and go to **Advanced Settings → Camera Account**. Create a username and password if you haven't already set up an account on this camera. This is a separate login from your TP-Link ID and is used for local ONVIF and RTSP access. [TP-Link camera account guide](https://www.tp-link.com/uk/support/faq/2790/)
2. Install with [DSM 6 Task Scheduler](docs/synology-dsm6.md). Use Python 3.8.6 or newer and the templates in [config](config/). Replace CAMERA_IP with the camera's IPv4 address and put the camera account credentials in `credentials.json`.
3. In Surveillance Station, use camera address 127.0.0.1, HTTP port 12020, and the same camera account credentials. Under advanced device settings, select RTSP over TCP and custom RTSP port 12554. Select camera-side event detection and preserve the camera settings. Use continuous recording and configure DS Cam notifications.
4. Review the [Surveillance Station mapping](docs/event-mapping.md) in a maintenance window. Test video and a real person event before relying on the installation.

For interactive use, run `python3 c120_onvif_proxy.py --camera CAMERA_IP`. The proxy prompts for the camera account. Stop it with Ctrl+C before starting a managed instance on the same ports.

## Limits

The listeners use IPv4 loopback. Camera connections use HTTP SOAP and RTSP/TCP. Events can be lost during an outage or an interrupted PullMessages exchange. The mapping is shared by camera profiles emitting the same topic.

The DS212+ is an older Synology NAS model. Newer models running more recent versions of DSM and Surveillance Station may handle these person detection events differently and may not need this workaround. Behavior on newer setups and with other cameras remains unverified.

Python 3.8.6 is the compatibility baseline because it is the newest version Synology lists in its official Python3 package for the DS212+ running DSM 6.2.4. Newer Python versions can be used where available. [Synology Python3 package for DSM 6.2](https://www.synology.com/en-global/dsm/packages/py3k?os_ver=6.2)

[Architecture](docs/architecture.md) | [Troubleshooting](docs/troubleshooting.md) | [Release checklist](docs/release-checklist.md)

This project is independent of Synology and TP-Link.
