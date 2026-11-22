# Surveillance Station event mapping

Use a maintenance window. The directory on the documented build is /var/packages/SurveillanceStation/target/device_pack/camera_support/. Record the camera profile actually selected in Surveillance Station, including whether it is Generic_ONVIF. Save camera settings and private, metadata-preserving copies of ONVIF_event.conf and TP-Link.conf outside the package tree. Label backups with the package versions and a timestamp. Save a copy of each edited file immediately after the change for rollback comparison.

## ONVIF_event.conf

Locate the single [MotionDetection] section and search for RuleEngine/PeopleDetector/People in the whole file. If there is an identical entry in the target section, reuse it. Stop if there are duplicate sections, conflicting definitions or an unfamiliar syntax. Add only the following entry to that section, keeping indentation consistent with neighboring rules.

```ini
# TP-Link Tapo C120 - person detection as motion
{RuleEngine/PeopleDetector/People;VideoSourceConfigurationToken*ReferenceToken&VideoAnalyticsConfigurationToken*ReferenceToken&Rule*string;IsPeople*boolean}
        trigger_type = STANDARD
        trigger_value = @ANY@&@ANY@&@ANY@;@BOOLEAN@
```

Review the unified diff and preserve ownership, mode, line endings and encoding. If the package requires a reload, restart Surveillance Station through Package Center within the maintenance window. Verify continuous recording, DS Cam person detection and a recording marker. Check other cameras emitting the same topic because the table is shared.

## Isolate the optional profile edit

The hardware proof combined this table change with an edited TP-Link.conf, while automatic detection selected a generic profile. Keep the active profile consistent across the baseline and event-table-only trial. Use the original TP-Link.conf from the same Device Pack version. If needed, evaluate the prior edit as a separate trial only after confirming that the active profile is [TP-Link*Tapo C120] and that {BasicSettings} has exactly one expected original onvif_md_topic entry. The prior edit used:

```ini
onvif_md_topic = tns1:RuleEngine/PeopleDetector/People;VideoSourceConfigurationToken*ReferenceToken&VideoAnalyticsConfigurationToken*ReferenceToken&Rule*string;IsPeople*boolean
```

Record any user-added api_specific setting separately so the test changes one setting at a time. Record the selected profile and video, notification and archive results. The need for the separate profile edit remains unverified.

## Rollback and updates

Restore the pre-change backup only if the current file still matches the saved post-change copy and the package version is unchanged. If other edits exist, use the before/after diff to undo only the exact block added or line replaced. Stop on ambiguity. Device Pack and Surveillance Station updates can replace either file. Retain the newer vendor content and reassess the mapping after updating. Reload the package if needed and verify recording after rollback.
