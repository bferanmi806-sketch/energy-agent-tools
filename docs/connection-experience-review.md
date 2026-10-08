# Connection experience review — October 8, 2026

Energy Agent Tools is integration infrastructure: a self-hosted entry point for
an existing agent. It does not create or host another assistant.

## Research scope

I inspected [Composio's current website](https://composio.dev/), its public
platform sign-in screen, and the published Connect Link screenshot in its
[authentication guide](https://docs.composio.dev/docs/tools-direct/authenticating-tools#hosted-authentication-connect-link).
The live dashboard redirected to platform sign-in. No authenticated Connect Apps
session or live provider consent was accessed; the following dashboard comparisons
use documented behaviour, not a claim of direct observation.

The public website separates existing-agent use from developer integration and
names the connection action clearly. Its sign-in screen offers email, Google and
GitHub for platform access. Its published Connect Link screenshot names the
provider, explains account access, and provides one Continue action. The guide
separates choosing a toolkit, its supported authentication method, and permissions;
its callback reports connection success or failure.

The [current quickstart](https://docs.composio.dev/docs/quickstart) gives an existing
agent a small discovery and execution surface rather than loading every provider
schema. The [account lifecycle guide](https://docs.composio.dev/docs/auth-configuration/connected-accounts)
describes user-scoped accounts, server-side credential injection and disabled
accounts that cannot execute. These patterns already have equivalents in our gateway.

## Changes to the actual web app

| Before | Release behaviour |
| --- | --- |
| Account forms and engineering catalogue mixed | Connect accounts is the default; Tool catalogue is separate, using actual gateway setup metadata |
| Home Assistant form above every provider | Its approved OAuth form appears in the selected provider detail |
| Nine navigation items in one setup group | Connect, Workspace and Advanced groups, with existing deep links preserved |
| Generic MCP JSON only | Codex CLI, Claude Code and HTTP client guidance, with ChatGPT's static-bearer limitation stated |
| No managed Account/Settings view | Actual identity, permissions, public URL, key expiry/status, key-management link and sign-out |
| Very small operational copy | Larger readable labels, descriptions and controls; responsive account selection |

Provider sign-in stays separate from gateway sign-in. Octopus uses the user's API
key; Home Assistant uses provider-hosted authorization only for operator-approved
instances. A verified account remains pending until mapped to an owned site.
Catalogue metadata never implies that an account is connected. Custom MCP keeps
its reviewed tool schemas, target policy and supported credential schemes.

The pine and daylight Energy Agent Tools identity is retained. No Composio branding,
provider-count claims, hosted-agent runtime or shared provider credentials were adopted.
