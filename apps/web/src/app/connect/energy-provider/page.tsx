import Link from "next/link";
import type { Metadata } from "next";
import { ShieldCheck, Waves, Zap } from "lucide-react";

export const metadata: Metadata = {
  title: "Connect your energy system · Energy Agent Tools",
  referrer: "no-referrer",
};

export default function EnergyProviderHandoff() {
  return (
    <main className="signin-page">
      <div className="signin-brand">
        <span className="brand-mark" aria-hidden="true"><Waves size={21} /></span>
        <span>Energy Agent Tools</span>
      </div>
      <section className="signin-panel" aria-labelledby="handoff-title">
        <div className="signin-symbol" aria-hidden="true"><Zap size={24} /></div>
        <h1 id="handoff-title">Connect your energy system</h1>
        <p className="signin-intro" role="status">Preparing your provider sign-in. This tab will continue to the application you selected.</p>
        <div className="signin-footnote">
          <ShieldCheck size={16} aria-hidden="true" />
          <p>Sign in directly with your energy provider and review its access request. After approval, Energy Agent Tools checks the selected site or system and returns you to the workspace.</p>
        </div>
        <p>If this page stays open, check the original dashboard tab for an error. After verification, open Connections to map the system to a site.</p>
        <Link className="button button-secondary" href="/?view=connections">Return to Connections</Link>
      </section>
    </main>
  );
}
