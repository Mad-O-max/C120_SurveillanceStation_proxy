# Troubleshooting

- **Authentication rejected**. Verify the camera account in Surveillance Station and credentials.json, and check NAS and camera time. Restart after correcting the credentials.
- **HTTP 2020 works, event port fails**. Check the configured upstream port and the subscription destination in the log. The camera used TCP 1024 for a live /event-1024_1024 subscription. The port can change and may refuse connections without a lease. An empty HTTP GET has little diagnostic value. An existing subscription also worked via /onvif/service on TCP 2020 in a different test.
- **No notification or marker**. Confirm the camera's AI settings, the PeopleDetector mapping, the active profile, recording and DS Cam notifications. Check that PullMessages and Renew succeed. Initialized notifications can carry valid detection changes.
- **503 during a pull**. The lease may already be busy or waiting for recovery. Check maintenance warnings and look for a recovery event. An interrupted PullMessages request can lose events.
- **RTSP resets**. Check custom RTSP port 12554, TCP and RTSP credentials. The relay logs the endpoint and read/write stage where known. One startup reset was observed with v3.1. Its ONVIF capture excluded RTSP.
- **Boot failure**. Check the runtime account, the absolute Python path, volume availability, service.log and proxy.log. A held lock with an unavailable controller can indicate an orphaned proxy after forced supervisor termination. Review the process owner and exact command before stopping that verified child. Retain the lock file and start the task again when status reports stopped.

Use diagnose to print service status and effective settings with the camera address omitted. Collect packet captures separately by explicit choice and for the shortest useful interval. Review and redact raw material locally before sharing it. Authorization headers, SOAP data, addresses and camera footage can identify the installation.
