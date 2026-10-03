# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

The user authorizes TypeScript/React or Next.js for the web application. Implementation choice is delegated by the full-ownership goal. Next.js hosts the interface and server-side SDK access; the Python gateway keeps energy calculations, provider connections and scope enforcement.

## Users

Developers and energy-system owners connecting their systems to AI agents through one gateway. The product must support self-hosting and ordinary energy questions without requiring knowledge of provider APIs or tool names.

## Product Purpose

Energy Agent Tools is an open-source, self-hostable Composio for energy. It connects heterogeneous energy systems and engineering tools through provider-independent capabilities, Python and TypeScript SDKs, and MCP.

## Operating Context

The user confirmed the first web journey starts with connecting a system, then mapping it to a site. Users browse Connect Apps, connect a provider, associate sites/assets and connect their agent. The gateway resolves providers and composes evidence-backed workflows such as forecasting consumption and estimating a bill.

## Capabilities and Constraints

The Python gateway and TypeScript SDK already support scoped discovery, capabilities, connections, artifacts, skills, jobs and MCP. Toolkit metadata comes from the registry. The initial interface needs a real authenticated gateway. Persistent identity and connection management are under development. Operator-provisioned credentials are an interim access method, not a finished normal-user onboarding experience.

Historical meter values are METERED, predictions FORECAST and costs CALCULATED FROM FORECAST. Synthetic fixtures do not qualify a physical meter or private provider. Provider secrets must stay out of browser storage and logs. Shared connections require explicit ACLs; no implicit cross-user sharing is permitted.

## Brand Commitments

Energy Agent Tools. Strong open-source product, not enterprise positioning. Clear factual language without invented customer, usage, deployment or accuracy claims.

## Evidence on Hand

Published Python core v0.3.0, qualified SDK contracts and reproducible local/CI evidence in docs/evidence. Physical-site access, broad held-out evaluation, independent review/contribution and sustained deployment remain open.

## Product Principles

- Connect systems before asking users to configure sites.
- Use the gateway's structured metadata and real persisted state.
- Keep units, provenance, uncertainty and provider limitations visible.
- Keep self-hosting first-class.
- A polished interface must expose supported actions and honest recovery states.

## Open assumptions

The first operational interface uses a light surface for daytime laptop work. This is an implementation assumption; no existing visual identity or user-provided artwork exists. The initial application keeps unavailable lifecycle actions explicit while the persistent control plane is integrated.
