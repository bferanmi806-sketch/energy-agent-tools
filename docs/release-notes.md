# Energy Agent Tools 0.5.0 — prepared release

Adds native read-only Tesla Energy and Enphase Energy authorization alongside
Home Assistant. Approved provider applications use server-side environment
secrets, bounded resource identifiers, provider-hosted login in a separate tab,
verification, workspace-owned site mapping and scoped agent execution. The SDK
adds `workspace().beginProviderAuthorization()`.

Confidential authorization-code exchange supports the providers' documented
client authentication and Tesla's regional audience. Existing PKCE and Home
Assistant protocols remain supported. Access and refresh tokens stay encrypted;
failed verification cannot activate a connection.

Tesla exposes site information and live status. Enphase exposes system summary
reads for eligible deployments. Both require registered applications and owner
consent; Enphase's internal-business-use restriction must be checked before use.
Neither integration has a documented server-side revocation endpoint. Local
disconnect deletes gateway access; users remove provider consent separately.

Python gateway, TypeScript SDK and web source use version 0.5.0. Synthetic
acceptance does not establish live provider qualification. See
[configuration](cloud-provider-oauth.md) and
[qualification requirements](cloud-provider-qualification.md) and the
[release readiness report](release-report-v050.md).

# Energy Agent Tools 0.4.0 — prepared release

Matching Python gateway, TypeScript SDK and self-hosted web application. Clearer
provider selection, verified connection-to-site flow, Account & settings and
client-specific agent onboarding. See the [release readiness report](release-report-v040.md)
for tests and remaining authentication and external qualification limits.

# Energy Agent Tools v0.3.0

This core release adds a provider-independent consumption forecast and bill
workflow, automatic historical and future weather context, aggregation of
high-resolution observed meter data, and partitioned CSV datasets with bounded
pages, streamed summaries, and backup/restore support. Synthetic qualification
includes a three-month to eight-day forecast and a one-minute meter fixture.

Physical-site forecast accuracy, the TypeScript SDK, the Connect Apps web
application, and the persistent hosted connection control plane remain open.
See the [v0.3.0 core report](release-report-v030.md) and
[release evidence](evidence/release-v030.json).

# Energy Agent Tools v0.2.0

The previous milestone added scoped capability resolution, reviewed workflow
composition, connector onboarding and engineering integrations. See the
[v0.2.0 report](release-report-v020.md) for its verification and remaining gates.

# Energy Agent Tools 0.1.0

Initial public preview of a self-hostable, search-first gateway to energy data and
engineering tools. Includes 18 actions across 10 configured toolkits, seven MCP
helpers, Python SDK/provider schema adapters, six energy workflow guides, private
local artifacts and conservative control policies.

HTTP integrations cover Carbon Intensity GB, Open-Meteo, Octopus consumption and
public tariffs, Home Assistant, Emoncms and Elexon. Local engineering includes
pvlib PVWatts, pandapower AC power flow, constrained battery scheduling and thermal
calculations. Existing MCP servers and fixed executables can be registered with
reviewed permissions and result semantics.

Local verification: 60 tests passed, Ruff checks/formatting passed, mypy passed
for 17 source files, wheel/source builds passed, and an isolated base-wheel install
passed. Five public live probes returned nonempty data. Private-provider tests
use fixtures; no real energy infrastructure was controlled. See
[verification](verification.md) for exact evidence and [catalogue](connectors.md)
for integration status.

This is a preview release. Interactive OAuth/token refresh, public multi-user
service authentication, isolated untrusted Python execution and several researched
energy engines remain deferred. The provided MCP service binds to local stdio or
loopback HTTP. Read [limitations](limitations.md) before deploying it.

Install from source with `uv sync --extra engineering`, or install the attached
wheel with `pip install energy_agent_tools-0.1.0-py3-none-any.whl`. Add the
`engineering` extra to install pvlib/pandapower. The repository is MIT licensed;
provider data and dependencies retain their own licences.
