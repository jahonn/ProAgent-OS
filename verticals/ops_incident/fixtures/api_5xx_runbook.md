# API 5xx Runbook

Harborline On-Call treats an elevated API 5xx error rate above 2 percent for 5 minutes as a sev2 incident.

Page the API primary. Check the gateway dashboard, then the upstream service error budget.

If the error rate stays above 2 percent after one rollback of the last deploy, escalate to sev1 and page the incident commander.

Customer data exposure or a complete outage is sev1. Do not declare the incident resolved until the error rate is back under 1 percent for 15 minutes.

Do not restart the database from this runbook.
