# Application memory reservations

SplatTop FastAPI, Celery worker and Redis previously requested no memory.
The September 8 00:03 UTC snapshot observed approximately 135 MiB per API
replica, 640 MiB for Celery and 423 MiB for Redis. Redis INFO reported a
639 MiB lifetime allocation peak. These measurements are baseline evidence,
not a full workload peak/percentile study.

Production now requests 256 MiB for each API replica, 1 GiB for the worker,
and 768 MiB plus 10m CPU for Redis. No memory or CPU limit is introduced.
Existing API/worker CPU reservations and concurrency stay unchanged. Requests
improve placement accounting; they do not reduce application memory use.

The added memory reservations total 2304 MiB. Before this slice, the two
nodes reserved approximately 7094 MiB out of 20049 MiB allocatable, so the
result is approximately 9398 MiB (47%) at that baseline. CPU increases by
10m, from 3259m to 3269m before other maintenance work. These cluster totals
are not sufficient alone: check each node's current requests, transient jobs,
affinity and rollout strategy immediately before activation.

Deploy after Redis has persistent storage and its migration is accepted.
Quiesce the Celery consumer before its no-surge rolling replacement. Keep
two ready API replicas through its rolling update. Verify ready endpoints,
actual pod placement, no Pending pods, Redis PING, preserved PVC identity,
public routes and resumed task completion after deployment. Reverting these
resource values must preserve the Redis persistence configuration.
