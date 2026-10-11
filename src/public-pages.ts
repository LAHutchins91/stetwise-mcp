import type { Express } from "express";
import { appBaseUrl } from "./config.js";
import { connectPageBody } from "./connect-page.js";

export const logo = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><rect width="256" height="256" rx="56" fill="#101520"/><path d="M48 128a80 80 0 1 0 160 0" fill="none" stroke="#8b7cff" stroke-width="20" stroke-linecap="round"/><path d="M176 96l32 32-32 32" fill="none" stroke="#5eead4" stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

export const page = (title: string, body: string) => `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title} · Stetwise by Ouroboros</title><link rel="icon" href="/icon.svg"><style>body{margin:0;background:#0b1020;color:#edf1ff;font:17px/1.65 system-ui}main{max-width:840px;margin:40px auto;padding:24px}a{color:#b8adff}h1{line-height:1.15;font-size:40px}h2{margin-top:32px}nav,footer{display:flex;flex-wrap:wrap;gap:18px}section{border:1px solid #39445f;border-radius:16px;padding:22px;margin:22px 0}input,textarea,button{font:inherit;box-sizing:border-box;max-width:100%;padding:10px;border:1px solid #66718b;border-radius:8px;background:#151d30;color:inherit}input,textarea{width:100%}label{display:block;margin:12px 0}button{cursor:pointer;margin:12px 8px 12px 0}code,pre{overflow-wrap:anywhere}pre{overflow:auto;background:#151d30;padding:12px;border-radius:8px}</style></head><body><main><nav><a href="/">Stetwise by Ouroboros</a><a href="/connect">Connect</a><a href="/support">Support</a></nav><h1>${title}</h1>${body}<footer><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/data">Your data</a><a href="mailto:ouroborosplugins@gmail.com">ouroborosplugins@gmail.com</a></footer></main></body></html>`;

export function installPublicPages(app: Express) {
  app.get("/access", (_req, res) =>
    res.type("html").send(
      page(
        "Stetwise access",
        `<p>Editing tools are available during the 14-day trial and on Pro. This connection does not have that access. It cannot start a purchase or add an overage charge.</p>
<p>Trial accounts can apply 100 edits and 20 recalculations per UTC month, and store 25 new files. Pro accounts can apply 1,000 edits, store 250 new files, and run 200 recalculations. Each file must be at most 25 MB (26,214,400 bytes). Past a cap, Stetwise blocks the call. Nothing extra is billed.</p>
<p><a href="/connections">Manage the connection</a> or <a href="/support">contact support</a> if this looks wrong.</p>`
      )
    )
  );
  app.get("/icon.svg", (_req, res) => res.type("svg").send(logo));
  app.get("/connect", (_req, res) => res.type("html").send(page("Connect an assistant", connectPageBody(appBaseUrl()))));
  app.get("/privacy", (_req, res) =>
    res.type("html").send(
      page(
        "Privacy policy",
        `<p>Effective October 11, 2026. Stetwise by Ouroboros is operated for Ouroboros. Contact <a href="mailto:ouroborosplugins@gmail.com">ouroborosplugins@gmail.com</a> or use the <a href="/support">support form</a>.</p>
<h2>What we process</h2>
<p>We process account email, subscription status, and the Office files you upload so an assistant you connect can preview and apply edits. We store immutable versions so you can undo. On a hosted install, file bytes live in a private Supabase Storage bucket. The service role on the server is the only key that reads them.</p>
<h2>Retention</h2>
<p>Files are deleted 24 hours after they were last used. You can set retention to 7 days (168 hours) instead. You can delete a file, or every file, sooner. Download links last 15 minutes. Upload links are one-time and last 15 minutes. Proposals expire after 30 minutes.</p>
<h2>Logs and training</h2>
<p>Logs contain operation names, hashes, and sizes. They do not contain file contents, cell values, or document text. File contents are not used to train models. We do not sell files.</p>
<h2>Subprocessors</h2>
<p>Supabase provides sign-in and, when configured, the database. Vercel may host the MCP and account pages. The engine host runs the file editor. Stripe processes payment. Google provides optional sign-in. We do not store complete card numbers.</p>
<h2>Caps</h2>
<p>Monthly caps limit applied edits, new files, and recalculations. Stetwise does not bill overage. A refused call is the whole response.</p>`
      )
    )
  );
  app.get("/terms", (_req, res) =>
    res.type("html").send(
      page(
        "Terms of service",
        `<p>Effective October 11, 2026. These terms cover Stetwise by Ouroboros. Questions go to <a href="mailto:ouroborosplugins@gmail.com">ouroborosplugins@gmail.com</a>.</p>
<h2>Your files</h2>
<p>You keep your rights in files you upload. You let us host, edit, and return them only to provide the service. Upload only files you may use. v1 edits .xlsx and .docx. If an edit cannot be done without risking the rest of the file, Stetwise refuses it.</p>
<h2>Paid access</h2>
<p>Tools require a trial or Pro. The 14-day trial and the billing interval are shown by Stripe before you pay. This service does not add usage charges when you pass a monthly cap; the call is blocked. Cancel in the billing portal. Cancellation does not by itself delete files. Deletion is separate, on <a href="/data">Your data</a>.</p>
<h2>Limits</h2>
<p>The assistant decides when to call a tool. Stetwise does not promise Excel-identical recalculation in v1, and it does not run macros. Review the diff before you apply. Keep your own copy of anything you cannot lose.</p>`
      )
    )
  );
  app.get("/support", (_req, res) =>
    res.type("html").send(
      page(
        "Contact Stetwise",
        `<p>Email <a href="mailto:ouroborosplugins@gmail.com">ouroborosplugins@gmail.com</a> or send a note here. Do not include passwords, card numbers, or a full workbook.</p>
<form id="support"><label>Reply email<input name="email" type="email" required maxlength="254"></label><label>How can we help?<textarea name="message" required minlength="10" maxlength="4000" rows="7"></textarea></label><button>Send request</button></form><p id="message" role="status"></p>
<script>document.getElementById('support').onsubmit=async function(e){e.preventDefault();var b=this.querySelector('button');b.disabled=true;try{var r=await fetch('/api/support',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:this.email.value,message:this.message.value})});var d=await r.json();if(!r.ok)throw Error(d.error||'Unable to send');document.getElementById('message').textContent='Request received. Reference: '+d.id;this.reset()}catch(err){document.getElementById('message').textContent=err.message}finally{b.disabled=false}};</script>`
      )
    )
  );
}
