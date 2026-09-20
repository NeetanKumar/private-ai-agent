# Incident Response Runbook

## Severity levels
SEV1 means a total outage of the customer-facing service; the on-call engineer must be paged within 5 minutes. SEV2 means a major feature is degraded; the team must respond within 30 minutes. SEV3 covers minor defects handled during business hours.

## Roles
The incident commander coordinates the response and owns communication. A scribe records the timeline.

## Communication
During a SEV1 the public status page is updated every 30 minutes until resolution.

## On-call
The on-call rotation hands over every Monday at 10:00. If the primary does not acknowledge a page within 15 minutes, the alert escalates to the secondary.

## After the incident
A blameless postmortem is published within 5 business days.
