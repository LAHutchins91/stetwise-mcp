import type { Express } from "express";
import { appBaseUrl } from "./config.js";
import { canonicalPublicOrigin } from "./public-url.js";

export function protectedResourceMetadata(baseUrl: string, supabaseUrl: string) {
  return {
    resource: `${baseUrl}/mcp`,
    resource_name: "Stetwise by Ouroboros",
    authorization_servers: supabaseUrl ? [`${supabaseUrl}/auth/v1`] : [],
    scopes_supported: ["email", "offline_access"],
    bearer_methods_supported: ["header"],
    resource_documentation: `${canonicalPublicOrigin(baseUrl)}/connect`
  };
}

export function installPluginAuth(app: Express, baseUrl: string, supabaseUrl: string, anonKey: string) {
  app.get(["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"], (_req, res) => {
    res.set("Access-Control-Allow-Origin", "*").json(protectedResourceMetadata(appBaseUrl(), supabaseUrl));
  });
  void baseUrl;
  app.get(["/oauth/consent", "/connections"], (_req, res) => {
    const config = JSON.stringify({ supabaseUrl, anonKey }).replace(/</g, "\\u003c");
    res.set({ "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY" });
    res.type("html").send(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Stetwise</title>
<style>body{margin:0;background:#0b1020;color:#edf1ff;font:17px/1.6 system-ui}main{max-width:560px;margin:8vh auto;padding:28px}a{color:#b4c5ff}button{font:inherit;padding:12px 20px;border-radius:10px;border:1px solid #637bb0;background:#2945a6;color:white;cursor:pointer;margin:8px 8px 8px 0}.secondary{background:transparent}.card{border:1px solid #39445f;border-radius:16px;padding:24px;margin:20px 0}#error{color:#ffb8b8}label{display:block;margin:12px 0}input{box-sizing:border-box;width:100%;padding:10px;font:inherit;border:1px solid #637bb0;border-radius:8px;background:#151d30;color:inherit}[hidden]{display:none!important}</style>
</head><body><main><a href="/">Stetwise by Ouroboros</a><h1 id="heading">Connect Stetwise</h1>
<p id="status" role="status">Checking your sign-in…</p><p id="error" role="alert"></p>
<button id="signout" class="secondary" hidden>Use a different account</button>
<form id="passwordForm" hidden><label>Email<input id="loginEmail" type="email" required autocomplete="username"></label><label>Password<input id="loginPassword" type="password" required autocomplete="current-password"></label><button>Sign in</button></form>
<section id="consent" class="card" hidden><h2 id="client"></h2><ul><li>Upload, read, and edit the Excel and Word files you send.</li><li>Save versions so an edit can be undone.</li><li>Read your email and whether your trial or Pro subscription is active.</li></ul>
<p>Monthly caps still apply. This connection cannot buy a plan or add an overage charge.</p>
<small id="destination"></small><small id="scopes"></small>
<button id="approve">Connect</button><button id="deny" class="secondary">Cancel</button></section>
<section id="connections" hidden><p>You can disconnect an application at any time.</p><div id="grants"></div></section>
<p><a href="/connections">Manage connected applications</a></p></main>
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.115.0/dist/umd/supabase.js"></script>
<script>
(async function(){
var cfg=${config}, id=new URLSearchParams(location.search).get('authorization_id');
var el=function(name){return document.getElementById(name)};
function fail(error){el('status').textContent='';el('error').textContent=error.message||String(error)}
try{
 if(!cfg.supabaseUrl||!window.supabase){el('status').textContent='Sign-in is not configured on this host yet.';return}
 var client=window.supabase.createClient(cfg.supabaseUrl,cfg.anonKey);
 var result=await client.auth.getSession();if(result.error)throw result.error;
 el('passwordForm').onsubmit=async function(e){e.preventDefault();var button=this.querySelector('button');button.disabled=true;try{var r=await client.auth.signInWithPassword({email:el('loginEmail').value,password:el('loginPassword').value});el('loginPassword').value='';if(r.error)throw Error('Email/password sign-in failed.');location.reload()}catch(err){fail(err);button.disabled=false}};
 if(!result.data.session){el('status').textContent='Sign in with the email and password for this account.';el('passwordForm').hidden=false;return}
 el('status').textContent='Signed in as '+result.data.session.user.email;
 el('signout').hidden=false;el('signout').onclick=async function(){this.disabled=true;try{var r=await client.auth.signOut({scope:'local'});if(r.error)throw r.error;location.reload()}catch(err){fail(err);this.disabled=false}};
 if(location.pathname==='/connections'){
  el('heading').textContent='Connected applications';el('connections').hidden=false;
  var grants=await client.auth.oauth.listGrants();if(grants.error)throw grants.error;
  var rows=Array.isArray(grants.data)?grants.data:(grants.data&&grants.data.grants)||[];
  if(!rows.length)el('grants').textContent='No applications are connected.';
  rows.forEach(function(grant){var row=document.createElement('div');row.className='card';var title=document.createElement('p');title.textContent=grant.client.name||grant.client.id;row.appendChild(title);var button=document.createElement('button');button.textContent='Disconnect';row.appendChild(button);button.onclick=async function(){button.disabled=true;try{var r=await client.auth.oauth.revokeGrant({clientId:grant.client.id});if(r.error)throw r.error;row.remove()}catch(err){fail(err);button.disabled=false}};el('grants').appendChild(row)});
  return;
 }
 if(!id)throw new Error('Open Stetwise from your assistant to start a new authorization request.');
 var details=await client.auth.oauth.getAuthorizationDetails(id);if(details.error)throw details.error;
 if(details.data.redirect_url){location.assign(details.data.redirect_url);return}
 el('client').textContent='Connect '+(details.data.client.name||'this application')+'?';
 el('destination').textContent='Return address: '+details.data.redirect_uri;
 el('scopes').textContent='Requested scopes: '+details.data.scope;
 el('consent').hidden=false;
 async function decide(approved){el('approve').disabled=true;el('deny').disabled=true;try{var r=await client.auth.oauth[approved?'approveAuthorization':'denyAuthorization'](id,{skipBrowserRedirect:true});if(r.error)throw r.error;location.assign(r.data.redirect_url)}catch(err){fail(err);el('approve').disabled=false;el('deny').disabled=false}}
 el('approve').onclick=function(){decide(true)};el('deny').onclick=function(){decide(false)};
}catch(error){fail(error)}
})();</script></body></html>`);
  });
}
