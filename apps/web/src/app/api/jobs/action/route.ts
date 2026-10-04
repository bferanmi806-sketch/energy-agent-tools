import { EnergyAgentTools, EnergyHttpError } from "@energy-agent-tools/sdk";
import { cookies } from "next/headers";
import { isAllowedMutationOrigin, openSession, readBoundedForm, readWebRuntimeConfig, SESSION_COOKIE_NAME } from "@/lib/security";

export const runtime = "nodejs";
function failure(status: number): Response {
  return Response.json({ ok: false, error: "job_action_failed" }, { status, headers: { "Cache-Control": "no-store" } });
}
export async function POST(request: Request): Promise<Response> {
  let config;
  try { config = readWebRuntimeConfig(process.env); } catch { return failure(503); }
  if (!isAllowedMutationOrigin(request.headers.get("origin"), config.webOrigin)) return failure(403);
  const form = await readBoundedForm(request);
  if (!form || [...form.keys()].some(name => name !== "job_id" && name !== "operation") || form.getAll("job_id").length !== 1 || form.getAll("operation").length !== 1) return failure(400);
  const jobId = form.get("job_id"), operation = form.get("operation");
  if (!jobId || !/^[a-f0-9]{32}$/.test(jobId) || (operation !== "result" && operation !== "cancel" && operation !== "delete")) return failure(400);
  const saved = openSession((await cookies()).get(SESSION_COOKIE_NAME)?.value, config.sessionKey.toString("hex"));
  if (!saved) return failure(401);
  const gateway = new EnergyAgentTools({ baseUrl: config.gatewayUrl, token: saved.token, fetch: (input, init) => fetch(input, { ...init, cache: "no-store" }) });
  try {
    const result = await gateway.jobAction(jobId, { operation });
    if (!result.ok) return failure(422);
    if (operation === "result") {
      return Response.json(result, { headers: { "Cache-Control": "no-store", "Content-Disposition": `attachment; filename="energy-job-${jobId}.json"`, "X-Content-Type-Options": "nosniff" } });
    }
    return new Response(null, { status: 303, headers: { Location: new URL("/?view=jobs", config.webOrigin.origin).toString(), "Cache-Control": "no-store" } });
  } catch (error) {
    return failure(error instanceof EnergyHttpError && (error.status === 401 || error.status === 403) ? error.status : 502);
  }
}
