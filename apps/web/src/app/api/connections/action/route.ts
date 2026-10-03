import { EnergyAgentTools, EnergyHttpError } from "@energy-agent-tools/sdk";
import { cookies } from "next/headers";
import { isAllowedMutationOrigin, openSession, readBoundedForm, readWebRuntimeConfig, SESSION_COOKIE_NAME } from "@/lib/security";

export const runtime = "nodejs";
function failure(status:number):Response {
  return Response.json({ok:false,error:"connection_action_failed"},{status,headers:{"Cache-Control":"no-store"}});
}
export async function POST(request:Request):Promise<Response> {
  let config;
  try {config=readWebRuntimeConfig(process.env);} catch {return failure(503);}
  if(!isAllowedMutationOrigin(request.headers.get("origin"),config.webOrigin))return failure(403);
  const form=await readBoundedForm(request);
  if(!form || [...form.keys()].some(name=>name!=="connection_id"&&name!=="action") || form.getAll("connection_id").length!==1 || form.getAll("action").length!==1) return failure(400);
  const connectionId=form.get("connection_id"),action=form.get("action");
  if(!connectionId || connectionId.length>256 || (action!=="verify"&&action!=="disconnect"))return failure(400);
  const saved=openSession((await cookies()).get(SESSION_COOKIE_NAME)?.value,config.sessionKey.toString("hex"));
  if(!saved)return failure(401);
  const gateway=new EnergyAgentTools({baseUrl:config.gatewayUrl,token:saved.token,fetch:(input,init)=>fetch(input,{...init,cache:"no-store"})});
  try {
    const identity=await gateway.identity();
    const owned=identity.sites.filter(site=>site.user_id===identity.user_id);
    const site=owned.find(site=>site.id===saved.siteId)??owned[0];
    if(!site)return failure(403);
    const session=await gateway.createSession({site_id:site.id});
    try {
      if(action==="verify") {
        const result=await session.verifyConnection(connectionId);
        return Response.json({ok:true,action,status:result.health.status,checked_at:result.health.checked_at},{headers:{"Cache-Control":"no-store"}});
      }
      await session.disconnectConnection(connectionId);
      return Response.json({ok:true,action},{headers:{"Cache-Control":"no-store"}});
    } finally {await session.close().catch(()=>undefined);}
  } catch(error) {
    return failure(error instanceof EnergyHttpError && error.status===401?401:422);
  }
}
