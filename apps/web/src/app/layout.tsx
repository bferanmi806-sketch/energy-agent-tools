import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Energy Agent Tools · Connect apps",
  description: "Connect energy systems to agents through one scoped gateway.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en-GB">
      <body>
        <script id="energy-design-contract" type="application/json" aria-hidden="true">
          {JSON.stringify({
            thesis: "A gateway workbench for connecting energy systems to agents.",
            ownWorld: "Precise connector catalogue, scoped records, readable status.",
            story: "Connect a system, map it to a site, then connect an agent.",
            firstViewport: "Searchable catalogue beside truthful setup details.",
            directionSeed: "a1beafa8",
            implementationStage: "review pending",
          })}
        </script>
        {children}
      </body>
    </html>
  );
}
