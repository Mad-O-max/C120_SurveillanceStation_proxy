# Install and manage on DSM 6

DSM 6 Task Scheduler supports triggered user-defined scripts under a selected DSM user. Use one Boot-up task. Before cutover, confirm that Boot-up and the runtime user are selectable on the target DS212+, and verify that the task can execute Python with the volume mounted. Reboot behavior of this service on the DS212+ is pending. [Synology DSM 6 Task Scheduler](https://kb.synology.com/vi-vn/DSM/help/DSM/AdminCenter/system_taskscheduler?version=6)

## Stage the files

Use a dedicated, non-administrator DSM account. As an administrator, stage the code in a directory readable by that account and writable only by administrators. Keep state on a local filesystem that supports POSIX ownership, advisory locks and Unix sockets. Substitute the volume and runtime account. The account needs traversal access to the parent directories.

```sh
mkdir -p /volume1/c120/app/scripts /volume1/c120/state
cp c120_onvif_proxy.py proxy_settings.py /volume1/c120/app/
cp scripts/service.py /volume1/c120/app/scripts/
chown -R root /volume1/c120/app
chmod 755 /volume1/c120/app /volume1/c120/app/scripts
chmod 644 /volume1/c120/app/*.py /volume1/c120/app/scripts/service.py
chown PROXY_USER /volume1/c120/state
chmod 700 /volume1/c120/state
```

As the runtime user, use a private editor and restrictive umask to create state/proxy.json and state/credentials.json from the templates. Set both files to mode 600. Credentials are plaintext protected by filesystem permissions. The service checks owner and permissions, and refuses credential symlinks. Keep state out of shared or synchronized folders. A supported DSM credential retrieval API for this script remains unverified.

Use the absolute path to the installed Python interpreter. Check the version and configuration as the runtime user.

```sh
/path/to/python3 -c 'import sys; print(sys.version); sys.exit(sys.version_info < (3, 8, 6))'
/path/to/python3 /volume1/c120/app/c120_onvif_proxy.py --self-test
/path/to/python3 /volume1/c120/app/c120_onvif_proxy.py --config /volume1/c120/state/proxy.json --credentials /volume1/c120/state/credentials.json --check
```

Configuration precedence is CLI, JSON file, then defaults. Camera IPv4 is mandatory. Use distinct loopback listener ports from 1024 through 65535, positive upstream ports, pull_timeout from 1 to 5 seconds, io_timeout from 2 to 10 seconds and greater than pull_timeout, lifetime from 30 to 3600 seconds, and renew_margin from 5 to 180 seconds. Supply the username and password through the prompts or the credential file.

## Register and operate

In Control Panel > Task Scheduler, create a Triggered Task > User-defined script. Select Boot-up and the runtime user. Set Task Settings to the command below. Stage it disabled if the existing proxy is active.

```sh
/path/to/python3 /volume1/c120/app/scripts/service.py --state /volume1/c120/state start
```

Replace start with status, stop, restart or diagnose for manual operations. Repeated starts share one process lock for the state directory. Keep the lock file in place while installed. The private control socket handles stop and status. After a crash, the supervisor retries after 5, 15 and 45 seconds, then exits if the retry budget is exhausted. A healthy five-minute run resets the budget. Listeners start while the camera is offline. Running status confirms listener startup. Verify camera readiness with a live video and detection check.

For cutover, record the active camera settings, save the preceding proxy files and record how to launch them. Check the new configuration before stopping the old proxy. In the agreed window, stop the old proxy, run the task manually, verify status and HTTP GET /healthz, check a live recording and trigger a person event. Enable the boot task after the manual check. If cutover fails, stop the new service, disable the new task, restore the recorded camera settings and start the previous proxy. Verify playback and detection after rollback.

For updates, stage and test the replacement files first. Keep the previous code available. Stop the service before copying replacement files into the app directory, retain both state JSON files, reuse the task and restart. For uninstall, disable and delete the task, stop the service, confirm status is stopped, and restore the desired camera endpoint or previous proxy. Use the [event mapping rollback](event-mapping.md) if the mapping changed during this installation. Remove this installation's app files. Decide separately whether to retain its state directory and credentials.

## Logs

Read state/proxy.log for protocol and relay status and state/service.log for lifecycle. Each keeps the current file plus three 512 KiB generations. Temporarily set log_level to DEBUG in proxy.json and restart. Restore INFO afterward and restart again.

DSM 6 Log Center supports browsing system logs and network syslog. The service uses rotating files while the local socket and routing on this NAS remain unverified. For acceptance, check /dev/log, /var/run/syslog and /var/run/log with ls -l. Run `logger -t c120-proxy-test 'DSM logging probe'` as the runtime user and verify receipt in the configured logs or Log Center. Record socket presence and actual receipt separately. [Synology DSM 6 Log Center](https://kb.synology.com/en-au/DSM/help/DSM/LogCenter/logcenter_desc?version=6)
