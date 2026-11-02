# Database Failover

Failover the primary database only when the primary is unreachable and the replica is healthy.

Promote the replica, point the application at the new primary, and watch replication lag until it is under 5 seconds.

A failover is sev2 unless customer data was lost, which is sev1 and needs a human incident commander before any announcement.

Do not fail over because of an API 5xx spike alone.
