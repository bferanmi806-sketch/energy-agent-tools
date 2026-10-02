# Bounded simulation jobs

`energy_agent_tools.jobs.JobManager` runs the local numerical tools that can
take long enough to deserve a durable job boundary. It stores the lifecycle in
SQLite, keeps input and result files under a private job directory, and starts
one fixed Python module worker per running job. A hosting layer can submit a
job, return its identifier, poll a scoped status, retrieve a completed result,
or cancel a pending or running job.

The public operation set is intentionally small:

| Operation | Tool | What it does |
| --- | --- | --- |
| `heat_loss` | `engineering.calculate_heat_loss` | Steady-state envelope and ventilation heat loss |
| `power_flow` | `engineering.run_power_flow` | Explicit local pandapower AC network calculation |
| `battery` | `engineering.schedule_battery_charging` | Bounded local battery optimisation |
| `solar` | `engineering.estimate_solar_generation` | Explicit local pvlib generation estimate |
| `network_power_flow` | `pypsa.power_flow` | Bounded explicit PyPSA AC network |
| `network_dispatch` | `pypsa.optimize_dispatch` | One-hour lossless linear economic dispatch with fixed HiGHS settings |

The worker receives only an operation and its JSON arguments. It does not
receive a provider, account, plugin, executable, Python module, command, or
filesystem path from the caller. Its registry contains the engineering
toolkit plus the fixed network adapters for PyPSA operations; the
operation-to-tool map is fixed in `job_worker.py`. Authentication
and provider data access remain in the normal gateway; simulation input must
be explicit numerical data.

## Lifecycle and recovery

Jobs use the states `pending`, `running`, `completed`, `failed`, `cancelled`,
and `interrupted`. `JobManager.run_pending()` claims at most two jobs at a
time and continues until its pending queue is empty. A fresh manager marks
rows left in `running` by a previous process as `interrupted`; queued rows stay
`pending` so the caller can choose whether to run them. Results are written
atomically and can be read after the manager process restarts.

Every status, result, list, and cancellation call requires both the original
`user_id` and `session_id`. A mismatch returns `job_access_denied`, including
when the caller knows the job identifier. The manager applies per-job,
per-user, and global input and output byte quotas as well as per-user and
global job-count quotas. Completed input envelopes are removed after terminal
processing; result files remain until the hosting layer calls scoped `delete`
or `cleanup`. Deletion removes the row and private job directory, so terminal
job and byte quotas can be reclaimed deliberately.

One live manager owns a root through an OS advisory lock in `manager.lock`.
Constructing a second manager for that root fails with `manager_locked`.
`await manager.aclose()` terminates and drains known workers before releasing
the lock; a process crash releases the OS lock automatically and the next
manager marks abandoned `running` rows as `interrupted`. `resume_scope(job_id,
user_id)` recovers only the original session and optional site identifiers for
the owner. A host must apply its current site permission before recreating that
session, and the normal status/result methods still require the exact original
user and session pair.

The manager starts workers with a new process group and a scrubbed environment
that contains only interpreter lookup, source lookup, a private temporary home,
and locale settings. Timeouts terminate the process group, first with a
graceful signal and then with a hard kill. Private directories are mode `0700`
and JSON files are mode `0600` on platforms that support these permissions.
The worker sets Matplotlib's documented [`MPL_IGNORE_SYSTEM_FONTS`](https://matplotlib.org/stable/install/environment_variables_faq.html)
option to use bundled fonts. This avoids a macOS system-font scan during
cold numerical imports while keeping the 30-second process deadline.
This path was checked with Matplotlib 3.11.2 and actual PyPSA 1.2.4/HiGHS
1.15.1; older Matplotlib versions may not honor that option.

Opening a job store from the four-operation schema migrates its operation
constraint atomically. Existing IDs, ownership, site scope, lifecycle, results
and indexes remain usable. Reopening the upgraded store does not repeat the
migration.

## What this boundary guarantees

The subprocess protects the hosting event loop from a crashed numerical
dependency, bounds the number of concurrent simulations, limits input and
output sizes, and makes cancellation and restart state explicit. It is a
process boundary rather than a security sandbox. The current implementation
does not impose an OS memory limit, CPU quota, network namespace, syscall
filter, container boundary, or filesystem MAC policy. Optional numerical
libraries must therefore be run in an OS or container sandbox when an
untrusted user can submit simulation input. The manager also does not claim
that the engineering models are hardware validated; their result kind,
assumptions, and warnings remain part of the returned `EnergyResult`.

## Example

```python
from pathlib import Path

from energy_agent_tools.jobs import JobManager, SimulationOperation

manager = JobManager(Path(".energy-agent-state/jobs"))
job = manager.submit(
    "user-1",
    "session-1",
    SimulationOperation.HEAT_LOSS,
    {
        "indoor_temp_c": 21,
        "outdoor_temp_c": 2,
        "components": [
            {"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2},
        ],
        "air_changes_per_hour": 0.4,
    },
)

# In an async host:
# await manager.run_pending()
# status = manager.status(job.job_id, "user-1", "session-1")
# result = manager.result(job.job_id, "user-1", "session-1")
```
