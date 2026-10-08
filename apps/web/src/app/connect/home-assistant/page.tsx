import Link from "next/link";
import { Home, ShieldCheck, Waves } from "lucide-react";

export default function HomeAssistantHandoff() {
  return (
    <main className="signin-page">
      <div className="signin-brand">
        <span className="brand-mark" aria-hidden="true"><Waves size={21} /></span>
        <span>Energy Agent Tools</span>
      </div>
      <section className="signin-panel" aria-labelledby="handoff-title">
        <div className="signin-symbol" aria-hidden="true"><Home size={24} /></div>
        <h1 id="handoff-title">Connect to Home Assistant</h1>
        <p className="signin-intro" role="status">Preparing your approved Home Assistant instance. Its sign-in page will open here when the gateway is ready.</p>
        <div className="signin-footnote">
          <ShieldCheck size={16} aria-hidden="true" />
          <p>Sign in on your own instance and review its authorization request. After authorization, return to Energy Agent Tools to verify the sensor and choose a site.</p>
        </div>
        <p>If this page stays open, check the original dashboard tab for an error.</p>
        <Link className="button button-secondary" href="/?view=connections">Return to connections</Link>
      </section>
    </main>
  );
}
