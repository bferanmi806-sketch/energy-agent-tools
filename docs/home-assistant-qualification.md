# Home Assistant protocol qualification

`scripts/qualify_home_assistant.py` qualifies the Home Assistant provider
against a real Home Assistant Docker instance. It is a protocol and boundary
qualification for CI. It does not claim that a physical meter, a real
household, or a device control path is connected.

The run uses the pinned official image
`ghcr.io/home-assistant/home-assistant@sha256:3e6710a7ab2a61311d9d899b719f6c3657791c63e8f4942cec4ebc42401d6b76`
(Home Assistant 2026.9.4). It creates a unique Docker volume and container,
writes a small configuration containing `homeassistant`, `api`, `onboarding`, and
`http`, and bounds the container to 1 GiB of memory, two CPUs, and 256
processes. The image is not started by the local development workflow when
the shared Docker VM is resource constrained; the same script is intended for
the Linux CI runner.

Run it from the repository root on a runner with Docker:

```sh
python scripts/qualify_home_assistant.py
```

The command prints one JSON object. A successful object contains protocol,
scope, semantic, freshness, and credential-storage evidence. It never
contains the generated owner password, authorization code, access token,
refresh token, provider response body, or container logs. A failed run prints
only a bounded error code and generic message and exits nonzero.

## Protocol exercised

The script follows Home Assistant's own onboarding and authentication
boundaries:

1. `GET /api/onboarding` is polled until the unfinished user step is available.
   The API status endpoint requires authentication, so it cannot be the first-run readiness check.
2. `GET /api/onboarding` confirms that onboarding is still available.
3. `POST /api/onboarding/users` creates a project-controlled development owner
   using a password generated in memory for this run.
4. `POST /auth/token` exchanges the returned authorization code using the
   same application client ID and the OAuth authorization-code grant, and
   validates both returned access and refresh tokens without printing either.
5. `POST /api/states/sensor.eat_qualification_power` seeds the synthetic
   state through the authenticated REST API, expecting HTTP 201 for a new entity.
6. `LocalProfile.connect` performs a second authenticated provider read and
   stores the token in the encrypted local vault.
7. `build_agent` reopens the profile and resolves the scoped
   `get_current_power` binding through `home_assistant.get_state`.
8. The agent executes the read with a 300-second freshness bound and checks
   the result's kind, unit, quantity shape, site, asset, observation time,
   and provenance.

The seed deliberately has `unit_of_measurement: kW` but no `state_class`.
Energy Agent Tools therefore classifies the reading as `estimated` and
`instantaneous`. The qualification fails if the provider or the gateway
silently promotes that synthetic state to a metered value.

The scoped binding is stored under the Home Assistant account metadata with
the qualification site and asset. The asset is associated with the connection
before the profile is reopened, so the resolver must preserve the account,
site, and asset boundary when selecting and executing the capability.

## Resource ownership and cleanup

The script generates a 32-hex-digit suffix for the container, helper
container, and volume. It removes those exact names in a `finally` block. It
does not call `docker system prune`, stop Docker or Colima, restart unrelated
services, or inspect logs. If cleanup cannot be proven, the command reports a
bounded failure instead of broadening the cleanup scope.

## Evidence limits

Passing evidence proves:

- the pinned Home Assistant image starts with the minimal configuration;
- the documented onboarding user endpoint returns an authorization code;
- the documented authorization-code token exchange returns an access token;
- authenticated state creation and reads work over the real REST API;
- the local encrypted vault survives the connection journey without storing
  the token in `profile.json` or as plaintext in the SQLite vault;
- reviewed capability scope, estimated semantics, observation freshness, and
  provenance survive a profile reopen and real provider execution.

It does not prove production-provider credentials, physical telemetry
quality, long-lived token refresh, a hardware integration, or control safety.
Those require an operator-owned Home Assistant installation and a separate
reviewed qualification.

The endpoint shapes used here are documented by Home Assistant's
[onboarding view source](https://raw.githubusercontent.com/home-assistant/core/2026.9.4/homeassistant/components/onboarding/views.py),
[authentication API](https://developers.home-assistant.io/docs/auth_api/), and
[REST API](https://developers.home-assistant.io/docs/api/rest/). The REST API
documentation also explains why the minimal configuration includes the
`api` integration when the frontend is not enabled.
