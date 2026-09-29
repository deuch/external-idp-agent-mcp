// Web client simulating the mobile app: OIDC Authorization Code + PKCE against Keycloak (myID),
// tokens kept in memory only. The app only ever holds a token for the BFF (APIM "chat" API);
// APIM obtains the token for the MCP server itself (On-Behalf-Of token exchange).
(async () => {
  const $ = (id) => document.getElementById(id);
  const cfg = await (await fetch("/config.json", { cache: "no-store" })).json();

  const mgr = new oidc.UserManager({
    authority: cfg.authority,
    client_id: cfg.clientId,
    redirect_uri: window.location.origin + "/",
    post_logout_redirect_uri: window.location.origin + "/",
    response_type: "code", // PKCE (S256) is used by default
    scope: "openid profile email",
    userStore: new oidc.WebStorageStateStore({ store: new oidc.InMemoryWebStorage() }),
    automaticSilentRenew: false,
  });

  let previousResponseId = null;

  async function currentUser() {
    let user = await mgr.getUser();
    if (user && user.expires_in !== undefined && user.expires_in < 30 && user.refresh_token) {
      user = await mgr.signinSilent(); // refresh_token grant, rotated by the IdP
    }
    return user && !user.expired ? user : null;
  }

  async function getToken() {
    const user = await currentUser();
    if (!user) throw new Error("Session expirée, reconnectez-vous.");
    return user.access_token;
  }

  async function callApi(path, options = {}) {
    const resp = await fetch(cfg.apiBaseUrl + path, {
      ...options,
      headers: { ...(options.headers || {}), Authorization: `Bearer ${await getToken()}` },
    });
    const text = await resp.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = { message: text }; }
    if (!resp.ok) {
      const detail = (data.error && data.error.message) || data.message || data.error || resp.statusText;
      throw new Error(`${resp.status} : ${detail}`);
    }
    return data;
  }

  function decode(token) {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const c = JSON.parse(atob(payload));
    return { sub: c.sub, preferred_username: c.preferred_username, aud: c.aud, azp: c.azp, scope: c.scope, exp: new Date(c.exp * 1000).toISOString() };
  }

  // Extracts the assistant text and the tool calls from a Responses API payload.
  function summarize(data) {
    const texts = [];
    const tools = [];
    for (const item of data.output || []) {
      if (item.type === "message") {
        for (const part of item.content || []) if (part.type === "output_text") texts.push(part.text);
      } else if (["function_call", "mcp_call", "custom_tool_call"].includes(item.type)) {
        tools.push({ name: item.name, arguments: item.arguments });
      }
    }
    return { text: texts.join("\n") || data.output_text || "", tools };
  }

  function addMessage(kind, text, toolCalls) {
    const div = document.createElement("div");
    div.className = `msg ${kind}`;
    div.textContent = text;
    if (toolCalls && toolCalls.length) {
      const tools = document.createElement("div");
      tools.className = "tools";
      tools.textContent = "🔧 " + toolCalls.map((t) => `${t.name}(${t.arguments || ""})`).join(", ");
      div.appendChild(tools);
    }
    $("chat").appendChild(div);
    div.scrollIntoView({ behavior: "smooth" });
  }

  async function refreshUi() {
    const user = await currentUser();
    $("login").hidden = !!user;
    $("logout").hidden = !user;
    $("composer").hidden = !user;
    if (!user) return;

    $("user-name").textContent = user.profile.name || user.profile.preferred_username || user.profile.sub;
    const me = await callApi("/me");
    $("debug-content").textContent = JSON.stringify({ appToken: decode(user.access_token), gatewayView: me }, null, 2);
  }

  if (location.search.includes("code=") && location.search.includes("state=")) {
    try {
      await mgr.signinRedirectCallback();
    } catch (e) {
      addMessage("error", `Échec de connexion : ${e.message}`);
    }
    history.replaceState({}, document.title, "/");
  }

  $("login").onclick = () => mgr.signinRedirect();
  $("logout").onclick = () => mgr.signoutRedirect();

  $("composer").onsubmit = async (event) => {
    event.preventDefault();
    const message = $("message").value.trim();
    if (!message) return;
    $("message").value = "";
    addMessage("user", message);

    try {
      const body = { input: message };
      if (previousResponseId) body.previous_response_id = previousResponseId;
      const data = await callApi("/responses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      previousResponseId = data.id || previousResponseId;
      const { text, tools } = summarize(data);
      addMessage("agent", text || "(pas de réponse)", tools);
    } catch (e) {
      addMessage("error", e.message);
    }
  };

  try {
    await refreshUi();
  } catch (e) {
    $("debug-content").textContent = `Erreur : ${e.message}`;
    $("debug").open = true;
    addMessage("error", `Impossible de charger la session : ${e.message}`);
  }
})();
