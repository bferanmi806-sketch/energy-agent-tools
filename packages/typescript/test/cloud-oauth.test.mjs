import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';
import { once } from 'node:events';
import {
  EnergyAgentTools,
  EnergyProtocolError,
} from '../dist/index.js';

const gatewayToken = 'cloud-oauth-gateway-token-private';
const authorization = {
  connection_id: 'managed-energy-connection',
  authorization_url: 'https://auth.tesla.com/oauth2/v3/authorize?response_type=code',
  state: 'one-time-state-value',
  expires_at: '2030-10-08T12:00:00Z',
};

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  return `http://127.0.0.1:${address.port}/api/v1`;
}

test('provider authorization uses its fixed gateway route and strict request and response contracts', async () => {
  const requests = [];
  let responseBody = { authorization };
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    requests.push({
      method: request.method,
      url: request.url,
      authorization: request.headers.authorization,
      body: Buffer.concat(chunks).toString('utf8'),
    });
    response.writeHead(201, { 'content-type': 'application/json' });
    response.end(JSON.stringify(responseBody));
  });
  const baseUrl = await listen(server);

  try {
    const workspace = new EnergyAgentTools({ baseUrl, token: gatewayToken }).workspace();
    const result = await workspace.beginProviderAuthorization({
      configuration_id: 'tesla-home',
      resource_id: '1234567890',
    });

    assert.deepEqual(result, { authorization });
    assert.deepEqual(requests[0], {
      method: 'POST',
      url: '/api/v1/workspace/provider-authorizations',
      authorization: `Bearer ${gatewayToken}`,
      body: '{"configuration_id":"tesla-home","resource_id":"1234567890"}',
    });

    const requestCount = requests.length;
    for (const resourceId of ['', 'abc', '-1', '1/2', '1'.repeat(33)]) {
      assert.throws(() => workspace.beginProviderAuthorization({
        configuration_id: 'tesla-home',
        resource_id: resourceId,
      }), EnergyProtocolError);
    }
    assert.throws(() => workspace.beginProviderAuthorization({
      configuration_id: 'tesla-home',
      resource_id: '1234567890',
      target_url: 'https://attacker.example/oauth',
    }), EnergyProtocolError);
    assert.equal(requests.length, requestCount);

    responseBody = {
      authorization: {
        ...authorization,
        access_token: 'private-access-token',
        refresh_token: 'private-refresh-token',
        client_secret: 'private-client-secret',
        api_key: 'private-application-key',
      },
    };
    await assert.rejects(
      workspace.beginProviderAuthorization({
        configuration_id: 'tesla-home',
        resource_id: '1234567890',
      }),
      (error) => {
        assert.ok(error instanceof EnergyProtocolError);
        assert.doesNotMatch(String(error), /private-(?:access-token|refresh-token|client-secret|application-key)/);
        return true;
      },
    );
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});
