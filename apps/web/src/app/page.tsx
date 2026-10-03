import Link from "next/link";
import { KeyRound, RefreshCw, ShieldCheck, Waves } from "lucide-react";
import { SignInForm } from "@/components/SignInForm";
import { Console } from "@/components/Console";
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
        <span className="brand-mark" aria-hidden="true"><Waves size={21} strokeWidth={1.8} /></span>
        <span>Energy Agent Tools</span>
      </div>
      <section className="signin-panel" aria-labelledby="signin-title">
        <div className="signin-symbol" aria-hidden="true"><KeyRound size={20} /></div>
        <h1 id="signin-title">Bring your energy systems into reach.</h1>
        <p className="signin-intro">Sign in to your self-hosted gateway to browse its real connector catalogue and scoped site data.</p>
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
        <span className="brand-mark" aria-hidden="true"><Waves size={21} strokeWidth={1.8} /></span>
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
  searchParams?: Promise<{ auth_error?: string | string[] }>;
}) {
  let state: DashboardState;
  try {
    state = await loadDashboard();
  } catch {
    state = { kind: "unavailable", message: "The gateway could not be reached. Check that it is running, then try again." };
  }

  switch (state.kind) {
    case "signed-out": {
      const params = searchParams ? await searchParams : undefined;
      return <SignIn message={authErrorMessage(params?.auth_error) ?? state.message} />;
    }
    case "ready":
      return <Console data={state.data} />;
    case "unavailable":
      return <Unavailable message={state.message} />;
    default: {
      const exhaustive: never = state;
      return exhaustive;
    }
  }
}
