---
category: system_calendar_sync
title: Calendar sync failing
severity: high
default_actions: restart_sync, note
keywords: calendar, sync, ical, feed, booking, availability, double booking, 503, CalendarSyncFailing
---
# Calendar sync failing

The sync job pulls each listing's iCal feed from Airbnb every 15 seconds and updates availability.
Failed syncs risk double bookings.

## Triage
1. `query_metrics calendar_sync` to see failure count vs success.
2. `search_logs "calendar sync failed"` for the error (HTTP 503 from the feed is usually upstream and transient).
3. Restart the sync job (low risk, internal, reversible). Note the incident.
4. If failures continue after the restart, escalate to the host to block dates manually.
