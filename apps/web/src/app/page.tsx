import Link from "next/link";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { SignInForm } from "@/components/SignInForm";
import { Console } from "@/components/Console";
import { ManagedConsole } from "@/components/ManagedConsole";
import { loadDashboard } from "@/lib/gateway";
import type { DashboardState } from "@/lib/types";

function authErrorMessage(code: string | string[] | undefined): string | null {
  if (code === "invalid_token") {
    return "That gateway access key was not accepted. Check the key with your operator and try again.";
  }
  if (code === "configuration") {
    return "The web gateway is not configured for sign-in. Ask the operator to check the gateway connection settings.";
  }
  if (code === "cookie_too_large") {
    return "The sign-in response was too large to store safely. Ask the operator to review the gateway identity configuration.";
  }
  return null;
}

function SignIn({ message }: { message: string | null }) {
  return (
    <main className="signin-page">
      <div className="signin-brand">
        <span className="brand-mark" aria-hidden="true"><img src="/brand/energy-mascot.png" width="31" height="31" alt="" /></span>
        <span>Energy Agent Tools</span>
      </div>
      <section className="signin-panel" aria-labelledby="signin-title">
        <img className="signin-mascot" src="/brand/energy-mascot.png" width="84" height="84" alt="" />
        <h1 id="signin-title">Sign in to your gateway</h1>
        <p className="signin-intro">Use the gateway access key supplied by your operator. Connect your own provider accounts after sign-in; provider API keys do not belong here.</p>
        {message ? (
          <div className="notice notice-error" role="alert">
            <span>{message}</span>
          </div>
        ) : null}
        <SignInForm />
        <div className="signin-footnote">
          <ShieldCheck size={16} aria-hidden="true" />
          <p>Access is provisioned by the gateway operator. This interface does not create accounts or store the key in browser storage.</p>
        </div>
      </section>
      <footer className="signin-footer">
        <span>Open-source gateway</span>
        <span aria-hidden="true">·</span>
        <span>Provider credentials stay with the gateway</span>
      </footer>
    </main>
  );
}

function Unavailable({ message }: { message: string }) {
  return (
    <main className="status-page">
      <div className="status-brand">
        <span className="brand-mark" aria-hidden="true"><img src="/brand/energy-mascot.png" width="31" height="31" alt="" /></span>
        <span>Energy Agent Tools</span>
      </div>
      <section className="status-panel" aria-labelledby="unavailable-title">
        <div className="status-icon status-icon-warning" aria-hidden="true"><RefreshCw size={20} /></div>
        <h1 id="unavailable-title">Gateway data is unavailable</h1>
        <p>{message}</p>
        <Link className="button button-primary" href="/">
          Retry connection <RefreshCw size={15} aria-hidden="true" />
        </Link>
      </section>
    </main>
  );
}

export default async function HomePage({
  searchParams,
}: {
  searchParams?: Promise<{ auth_error?: string | string[]; oauth?: string | string[]; view?: string | string[]; activity_before?: string | string[]; job_before?: string | string[]; job_status?: string | string[] }>;
}) {
  const params = searchParams ? await searchParams : undefined;
  const cursor = params?.activity_before;
  const activityBefore = typeof cursor === "string" && /^[1-9][0-9]*$/.test(cursor) && Number.isSafeInteger(Number(cursor))
    ? Number(cursor) : undefined;
  const jobBefore = typeof params?.job_before === "string" && /^[A-Za-z0-9_-]{1,256}$/.test(params.job_before) ? params.job_before : undefined;
  const rawStatus = params?.job_status;
  const jobStatus = rawStatus === "pending" || rawStatus === "running" || rawStatus === "completed" || rawStatus === "failed" || rawStatus === "cancelled" || rawStatus === "interrupted" ? rawStatus : undefined;
  let state: DashboardState;
  try {
    state = await loadDashboard(activityBefore, { ...(jobBefore ? { before: jobBefore } : {}), ...(jobStatus ? { status: jobStatus } : {}) });
  } catch {
    state = { kind: "unavailable", message: "The gateway could not be reached. Check that it is running, then try again." };
  }

  switch (state.kind) {
    case "signed-out": {
      return <SignIn message={authErrorMessage(params?.auth_error) ?? state.message} />;
    }
    case "ready": {
      if (state.data.kind === "managed") {
        const canManageCustomMCP = state.data.identity.can_manage_workspace === true && state.data.workspace.mode === "managed";
        const initialView = canManageCustomMCP && params?.view === "mcp"
          ? "mcp"
          : params?.view === "connections" || params?.view === "sites" || params?.view === "sharing" || params?.view === "agent" || params?.view === "skills" || params?.view === "activity" || params?.view === "jobs" || params?.view === "settings"
            ? params.view
            : "apps";
        const authorizationResult = params?.oauth === "connected" || params?.oauth === "cancelled" || params?.oauth === "invalid" || params?.oauth === "failed" || params?.oauth === "provider_connected"
          ? params.oauth
          : undefined;
        return <ManagedConsole data={state.data} initialView={initialView} {...(jobStatus ? { jobStatus } : {})} {...(authorizationResult ? { authorizationResult } : {})} />;
      }
      return <Console data={state.data} {...(jobStatus ? { jobStatus } : {})} initialView={params?.view === "connections" || params?.view === "skills" || params?.view === "activity" || params?.view === "jobs" || params?.view === "agents" || params?.view === "settings" ? params.view : "apps"} />;
    }
    case "unavailable":
      return <Unavailable message={state.message} />;
    default: {
      const exhaustive: never = state;
      return exhaustive;
    }
  }
}
