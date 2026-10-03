import {
  isAllowedMutationOrigin,
  resolveWebOrigin,
  serializeClearedSessionCookie,
} from "@/lib/security";

export const runtime = "nodejs";

function forbidden(): Response {
  return new Response("Forbidden", {
    status: 403,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/plain; charset=utf-8",
    },
  });
}

function redirectHome(cookie?: string): Response {
  const headers = new Headers({
    "Cache-Control": "no-store",
    Location: "/",
  });
  if (cookie !== undefined) headers.append("Set-Cookie", cookie);
  return new Response(null, { status: 303, headers });
}

export async function POST(request: Request): Promise<Response> {
  const originHeader = request.headers.get("origin");
  if (originHeader === null) return forbidden();

  let expectedOrigin;
  try {
    expectedOrigin = resolveWebOrigin(process.env.ENERGY_WEB_ORIGIN);
  } catch {
    return new Response("The web security configuration is invalid.", {
      status: 503,
      headers: {
        "Cache-Control": "no-store",
        "Content-Type": "text/plain; charset=utf-8",
      },
    });
  }
  if (!isAllowedMutationOrigin(originHeader, expectedOrigin)) return forbidden();

  let secureOrigin;
  try {
    secureOrigin = resolveWebOrigin(process.env.ENERGY_WEB_ORIGIN, process.env.NODE_ENV === "production");
  } catch {
    return new Response("The web security configuration is invalid.", {
      status: 503,
      headers: {
        "Cache-Control": "no-store",
        "Content-Type": "text/plain; charset=utf-8",
      },
    });
  }

  return redirectHome(serializeClearedSessionCookie(secureOrigin.secure));
}
