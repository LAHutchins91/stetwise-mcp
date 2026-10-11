import { canonicalPublicOrigin } from "./public-url.js";

function htmlEscape(value: string) {
  return value.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch] ?? ch);
}

export function landingConnectLead(baseUrl: string) {
  const origin = canonicalPublicOrigin(baseUrl);
  const mcp = htmlEscape(`${origin}/mcp`);
  return `<p>MCP address: <code>${mcp}</code>. <a href="${htmlEscape(`${origin}/connect`)}">Connect an assistant</a>.</p>`;
}

export function connectPageBody(baseUrl: string) {
  const mcp = htmlEscape(`${canonicalPublicOrigin(baseUrl)}/mcp`);
  return `<p>Stetwise by Ouroboros is one OAuth-protected editor for Excel and Word files. ChatGPT, Claude, Cursor, and Grok use the same server. Tools require your own trial or Pro subscription. An assistant cannot change your plan. Stripe shows the amount before you pay. There is no overage charge past the monthly caps.</p>
<section><h2>Shared connection</h2>
<p>MCP address:</p><p><code>${mcp}</code></p>
<p>Transport: Streamable HTTP. Sign in when the assistant opens OAuth. Do not paste an access token or password into a header or a chat. Revoke a host from <a href="/connections">connected applications</a>.</p>
</section>
<section><h2>ChatGPT</h2>
<ol><li>Enable developer mode if your workspace requires it for custom connectors.</li><li>Create a custom app, paste the MCP address, and choose OAuth.</li><li>Attach a workbook or document. The import tool accepts the file reference.</li><li>If a phone client omits the download URL, use the upload link from <code>start_upload</code> instead.</li></ol>
</section>
<section><h2>Claude</h2>
<p>Claude.ai does not hand a chat attachment to a remote connector. Ask Claude to call <code>start_upload</code>, then upload with curl from the sandbox or your computer:</p>
<pre><code>curl -T ./file.xlsx "PASTE_THE_UPLOAD_URL"</code></pre>
<p>Add the connector under Customize → Connectors, choose automatic OAuth registration, and leave client id and secret empty. Claude Code:</p>
<pre><code>claude mcp add --transport http stetwise ${mcp}</code></pre>
</section>
<section><h2>Cursor and Grok</h2>
<p>Add the MCP address as a remote HTTP server and approve OAuth. Upload with the same curl command, edit through <code>propose_edit</code> then <code>apply_edit</code>, and download with the link from <code>export</code>. Use those links as returned. A hosted link is a short-lived Supabase Storage URL: after the PUT, call <code>complete_upload</code>. A local or dev link is on this server, and the PUT response is the file record.</p>
</section>
<p>The planned public name stetwise.app is not served by this repository. Use the MCP address of the host you actually run.</p>`;
}
