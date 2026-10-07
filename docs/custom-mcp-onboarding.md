# Connect a custom MCP server

Use this flow to add selected tools from an HTTP MCP server to an owned site.
The gateway checks the approved schema before it publishes the tools.

Start with a [managed gateway](self-hosting.md) and the
[web console](../apps/web/README.md). Sign in with a workspace management key.
Obtain the MCP endpoint and provider credential from the server operator.

1. Open **Add MCP server**.
2. Enter a connection name and the MCP server URL.
3. Select the authentication method. Enter the provider credential when required.
4. Click **Inspect tools**. The page displays tool names and schema hashes.
5. Select the tools you want to expose. No tools are selected automatically.
6. Declare each selected tool's actions, result kind, and unit.
7. Confirm each review after you check the server's documented behavior and schema.
8. Click **Save pending connection**. The page opens **Connections**.
9. Choose the owned site and click **Map connection**.
10. Confirm that the connection is **Active**. Use **Connect my agent** to create a site-scoped client configuration.

Inspection does not grant execution access. A pending connection stays disabled
until mapping verifies the current schema. The default endpoint policy accepts
public HTTPS URLs. Private endpoints require operator approval in the embedded
host configuration. Do not put a username, password, or token in the URL.

Use **Metered** only for a source whose measurements you have verified.
Use **Estimated**, **Calculated**, **Simulated**, or **Forecast** when that label
matches the tool's output. Upstream MCP annotations do not establish those facts.

The browser holds the provider credential in component memory during review.
Saving sends the credential to the gateway's encrypted store. Discarding the
review, cancelling a request, or leaving the page clears the component state.
The browser does not save provider credentials in local storage or the session cookie.

## Verify or disconnect a connection

In **Connections**, click **Check connection** to compare the current MCP schema
with the saved review. A healthy check confirms connectivity and the reviewed
schema. It does not validate measurement accuracy or physical safety.

If the server changes its schema, disconnect the old connection and inspect the
server again. The gateway refuses unapproved schema changes. Credentials must be
entered again for a new connection.

To remove execution access, click **Disconnect**, then **Confirm disconnect**.
The gateway retains the disconnected record and removes its private tools.
Existing sessions cannot continue to execute those tools.

Active definitions recover when the managed gateway starts. Recovery checks the
current network policy and schema before restoring a private namespace. If a
provider is unavailable, its tools stay unavailable while other connections recover.

For API and SDK use, see the [MCP review contract](mcp-review.md).
Managed custom-MCP OAuth and a web editor for private endpoint approval are not available.
