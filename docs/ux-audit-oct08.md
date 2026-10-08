# UX audit — 8 October 2026

The first connection now advances from verification to site creation automatically. A new workspace reaches an active connection in **3 actions instead of 6**, with no manual reload. Agent instructions survive hiding the one-time key and can be reopened for existing keys.

Baseline: `e9438307a75570306f732ab1dc28c939c211f315`, fetched from `origin/main` before editing. This pass changes the web interface and an original brand asset; provider protocols, storage, permissions and gateway architecture remain intact. Python, SDK and web versions remain aligned at 0.5.0. Existing draft release packages target an earlier commit and do not contain this source update.

## Method and limits

Navigated the production Next application in the real in-app browser before inspecting the affected implementation. Used separate loopback gateway/state/cookies from the user's manual test environment. Every audit account, meter identifier, sensor and reading was fictional. The original manual environment remains available at `http://127.0.0.1:43002/` and makes real provider requests.

Tested a fresh workspace and returning users, at an observed 390×844 mobile viewport and desktop sizes (fresh desktop tab: 1280×720). Browser viewport overrides did not resize every existing tab consistently; sizes above are observed DOM measurements, not requested emulation sizes. Screenshots include full-page content and therefore can exceed viewport height. This was an agent-led usability inspection, not a recruited-user study or full WCAG certification.

## Friction and fixes

| Finding observed in browser | Improvement |
| --- | --- |
| A successful API-key submission saved the connection in the gateway, but Connections used an old frontend snapshot and needed a reload. | Clear the credential field and navigate directly to a freshly loaded Connections page. Navigation loads current gateway data and preserves browser back history. |
| First site required expanding a form, typing an IANA timezone, creating it, then separately mapping it. | Show the first-site form immediately; default browser timezone with UTC fallback; “Create site and connect” performs both operations. Timezone remains editable under Advanced settings. |
| Hiding a new key removed all setup guidance; returning users could not reopen it. | Keep token-free instructions and provide View setup for existing keys. Client choice shows one guide at a time. Explicit site grants and one-time token handling remain. |
| Registry/runtime identifiers and optional sensor mapping occupied the main connection path. | Move them into native accessible disclosures. Connection cards emphasize provider/site; internal references remain available in Connection details. |
| An unconfigured Tesla entry said Connect; disconnected cards offered no next action. | Show Setup required when no approved native OAuth configuration exists; add Connect again and actionable failed-health guidance. |
| Settings repeated its title and placed internal IDs ahead of useful controls. | Lead with the workspace, permissions, gateway URL and agent access; disclose technical account IDs. |
| Brand lacked the approved glowing direction. | Add an original mint/teal energy spark with a warm core and transparent background, distinct from Ghostty branding. |

## Measured actions

Counts include platform sign-in, with Octopus already selected, in a fresh workspace. Typing and scrolling are not clicks. Failed attempts, privacy-only Hide key checks, and external client setup are excluded. Explicitly choosing the already-selected provider would add one action to both counts.

| Milestone | Before | After |
| --- | --- | --- |
| Sign-in → verified provider → first site → active connection | 6 actions + 1 reload; 6 typed fields | 3 actions, no reload; 5 typed fields |
| Active connection → named, explicitly site-scoped agent key | 3 additional actions; 1 typed field | 3 additional actions; 1 typed field |
| Returning user opens guidance for an existing key, from agent page | Unavailable | 1 action |

The six baseline connection actions were Continue, Verify, Connections, expand site form, Create site, Map. The three after actions were Continue, Verify, Create site and connect. Key creation still requires selecting the site, deliberately. Issuing a key or copying configuration is not proof that an external agent connected.

## Composio comparison

Read the current [public website](https://composio.dev/) and [authentication documentation](https://docs.composio.dev/docs/authentication). Its public Get Started entry resolved to the already-authenticated dashboard session; read its navigation and client-specific onboarding without installing clients, authorizing an app, creating an account, or inspecting secret settings. Compared its single app action, provider-hosted consent handoff and client-specific guides against our browser journey. Adopted those interaction patterns while keeping self-hosted operator registration and owner-specific credentials explicit. Composio branding and its unrelated agent runtime are not copied.

## Validation

- Production web build, TypeScript check and all **22 web tests passed** on the final implementation. Tests include real gateway HTTP journeys, zero-site management, read-only scoped keys, encrypted cookies, request bounds, custom MCP recovery, and session-bound Home Assistant/Tesla/Enphase protocol fixtures.
- Browser: valid/invalid sign-in; provider/catalogue separation; empty search and clear recovery; valid/invalid API key; automatic first-site mapping; existing-site mapping; cancelled OAuth and restart; approved fictional Home Assistant consent/callback/sensor verification; returning key instructions; all three client guides and copy feedback; Settings URL copy and key status; disconnect cancellation/confirmation/reconnection; empty Jobs; successful Activity log; normal Back navigation.
- Injected a fictional mapping outage after site creation. The UI retained the created site and exposed Map connection retry. Recovery succeeded; site count stayed **2 → 2**.
- Simulated provider-side HTTP 401. Health check showed “Connection needs attention”; disconnect immediately removed saved access. New credentials restored the connection.
- Used an actual MCP client with keys issued in the UI, including the complete first-time mobile journey. Discovered **11 gateway tools**; searched and executed Octopus consumption; received **1.25 kWh** with units/provenance. Activity log showed Succeeded. Full results are returned to the client; Activity log is execution metadata, not a historical result-payload viewer.
- Cross-workspace MCP request returned **403**. After UI key revocation, that key returned **401**. Hiding the raw token removed it from document text; returning setup used placeholders rather than fetching the secret.
- Labels and disclosures were available in the accessibility tree; keyboard activation worked throughout the audited journey. Client buttons showed a visible 2px focus outline. Mobile page and agent-guide document widths stayed within the observed 390px viewport. Existing short-desktop sidebar scrolling remains intact.

## Evidence

| Screen | Before | After |
| --- | --- | --- |
| Sign-in | [Before](evidence/ux-oct08/before-signin.png) | [After](evidence/ux-oct08/after-signin.png) |
| Provider setup | [Before](evidence/ux-oct08/before-connect-desktop.png) | [After](evidence/ux-oct08/after-wide-desktop.png) |
| First site | [Before](evidence/ux-oct08/before-mapping.png) | [After](evidence/ux-oct08/after-mapping.png) |
| Mobile provider form | [Before](evidence/ux-oct08/before-mobile.png) | [After](evidence/ux-oct08/after-mobile-octopus.png) |

Additional screenshots: [desktop agent guidance](evidence/ux-oct08/after-agent-desktop.png), [mobile agent guidance](evidence/ux-oct08/after-mobile-agent.png), [mobile Settings](evidence/ux-oct08/after-mobile-settings.png), [mobile execution activity](evidence/ux-oct08/after-mobile-activity.png). [Measurements](evidence/ux-oct08/measurements.json) contain no credentials.

## Remaining blockers and evidence gaps

Live Tesla and Enphase account consent/read access still requires approved application configuration, exact HTTPS callbacks, provider-specific registration and user accounts. Enphase's license restriction remains. Local fictional OAuth and HTTP protocol tests do not qualify live providers. Normal users still need their provider's numeric resource/entity identifiers where discovery is unavailable; the interface does not fabricate automatic discovery or OAuth support.

Operator-provisioned gateway sign-in and provider-app configuration remain operator tasks. No actual Codex/Claude installation or configuration was changed; an independent MCP client proved gateway execution. Full screen-reader, contrast/zoom, real touch-device and recruited-user studies remain future evidence. Some catalogue terminology and advanced simulation copy remain technical. These limitations do not block the verified connection path, but should not be reported as completed external qualification.
