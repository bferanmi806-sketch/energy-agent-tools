"use client";

import { useState, type FormEvent, type ReactNode } from "react";

type Mutation = {kind:"idle"} | {kind:"pending"} | {kind:"error"};
export function GatewayForm({action,className,children,pendingLabel}: {
  action:"/api/site" | "/api/logout"; className?:string; children:ReactNode; pendingLabel:string;
}) {
  const [mutation,setMutation] = useState<Mutation>({kind:"idle"});
  async function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mutation.kind === "pending") return;
    const body = new URLSearchParams();
    for (const [name,value] of new FormData(event.currentTarget)) {
      if (typeof value !== "string") {setMutation({kind:"error"}); return;}
      body.append(name,value);
    }
    setMutation({kind:"pending"});
    try {
      const response = await fetch(action,{method:"POST",body,credentials:"same-origin",cache:"no-store",redirect:"follow"});
      if (!response.ok) throw new Error("Gateway mutation refused.");
      const destination = new URL(response.url);
      if (destination.origin !== window.location.origin || destination.pathname !== "/") throw new Error("Gateway mutation refused.");
      window.location.assign(destination.pathname + destination.search);
    } catch {setMutation({kind:"error"});}
  }
  return <form action={action} method="post" className={className} onSubmit={submit} aria-busy={mutation.kind === "pending"}>
    <fieldset disabled={mutation.kind === "pending"} style={{display:"contents"}}>{children}</fieldset>
    {mutation.kind === "pending" ? <p className="mutation-feedback" role="status">{pendingLabel}</p> : null}
    {mutation.kind === "error" ? <p className="mutation-feedback" role="alert">The request could not be completed. Check the gateway and try again.</p> : null}
  </form>;
}
