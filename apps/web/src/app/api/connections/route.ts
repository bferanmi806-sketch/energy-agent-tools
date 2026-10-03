import { EnergyAgentTools, EnergyHttpError } from "@energy-agent-tools/sdk";
import { cookies } from "next/headers";
import { isAllowedMutationOrigin, openSession, readBoundedForm, readWebRuntimeConfig, SESSION_COOKIE_NAME } from "@/lib/security";

export const runtime = "nodejs";
function failure(status: number): Response {
  return Response.json({ok:false,error:"connection_failed"},{status,headers:{"Cache-Control":"no-store"}});
}
export async function POST(request: Request): Promise<Response> {
  let config;
  try { config = readWebRuntimeConfig(process.env); } catch { return failure(503); }
  if (!isAllowedMutationOrigin(request.headers.get("origin"),config.webOrigin)) return failure(403);
  const form = await readBoundedForm(request);
  const fields = ["provider","credential","mpan","serial_number"];
  if (!form || [...form.keys()].some(name => !fields.includes(name)) || fields.some(name => form.getAll(name).length !== 1)) return failure(400);
  const provider = form.get("provider"), credential = form.get("credential"), mpan = form.get("mpan"), serial_number = form.get("serial_number");
  if (provider !== "octopus" || !credential || !mpan || !serial_number) return failure(400);
  const encrypted = (await cookies()).get(SESSION_COOKIE_NAME)?.value;
  const saved = openSession(encrypted,config.sessionKey.toString("hex"));
  if (!saved) return failure(401);
  const gateway = new EnergyAgentTools({baseUrl:config.gatewayUrl,token:saved.token,fetch:(input,init)=>fetch(input,{...init,cache:"no-store"})});
  try {
    const identity = await gateway.identity();
    const owned = identity.sites.filter(site=>site.user_id===identity.user_id);
    const site = owned.find(site=>site.id===saved.siteId) ?? owned[0];
    if (!site) return failure(403);
    const session = await gateway.createSession({site_id:site.id});
    try { await session.connectAccount({provider,credential,mpan,serial_number}); }
    finally { await session.close().catch(()=>undefined); }
    return Response.json({ok:true},{status:201,headers:{"Cache-Control":"no-store"}});
  } catch (error) {
    return failure(error instanceof EnergyHttpError && error.status === 401 ? 401 : 422);
  }
}
