"use client";

import { useState, type FormEvent } from "react";
import { ArrowRight, LoaderCircle } from "lucide-react";

type Submission = {kind:"idle"} | {kind:"pending"} | {kind:"error"; message:string};

export function SignInForm() {
  const [submission, setSubmission] = useState<Submission>({kind:"idle"});
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submission.kind === "pending") return;
    const token = new FormData(event.currentTarget).get("token");
    if (typeof token !== "string" || !token || token.length > 4096) {
      setSubmission({kind:"error",message:"Enter a valid gateway access key."});
      return;
    }
    setSubmission({kind:"pending"});
    try {
      const response = await fetch("/api/auth", {
        method:"POST", body:new URLSearchParams({token}), credentials:"same-origin",
        cache:"no-store", redirect:"follow",
      });
      if (!response.ok) throw new Error("Sign-in refused.");
      const destination = new URL(response.url);
      if (destination.origin !== window.location.origin || destination.pathname !== "/") {
        throw new Error("Sign-in refused.");
      }
      window.location.assign(destination.pathname + destination.search);
    } catch {
      setSubmission({kind:"error",message:"Sign-in could not be completed. Check the gateway and try again."});
    }
  }
  return (
    <form className="signin-form" action="/api/auth" method="post" onSubmit={submit} aria-busy={submission.kind === "pending"}>
      <label htmlFor="gateway-token">Gateway access key</label>
      <input id="gateway-token" name="token" type="password" autoComplete="current-password"
        required maxLength={4096} spellCheck={false} placeholder="Enter the key provided by your operator" />
      <button className="button button-primary signin-submit" type="submit" disabled={submission.kind === "pending"}>
        {submission.kind === "pending" ? <>Signing in <LoaderCircle size={16} aria-hidden="true" /></> : <>Continue to gateway <ArrowRight size={16} aria-hidden="true" /></>}
      </button>
      {submission.kind === "error" ? <p className="notice notice-error" role="alert">{submission.message}</p> : null}
    </form>
  );
}
