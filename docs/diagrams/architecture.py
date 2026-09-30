"""Generates architecture.svg: overview of the POC architecture (used by the main README)."""

from html import escape
from pathlib import Path

W, H = 1700, 860
FONT = "Arial, Helvetica, sans-serif"
out: list[str] = []


def text(x, y, lines, size=15, weight="normal", color="#202124", anchor="middle", lh=None):
    lh = lh or size * 1.35
    if isinstance(lines, str):
        lines = [lines]
    y0 = y - (len(lines) - 1) * lh / 2
    spans = "".join(f'<tspan x="{x}" y="{y0 + i * lh:.1f}">{escape(l)}</tspan>' for i, l in enumerate(lines))
    out.append(
        f'<text font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{color}" '
        f'text-anchor="{anchor}" dominant-baseline="middle">{spans}</text>'
    )


def rect(x, y, w, h, fill, stroke, rx=10, dash=None, sw=2):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')


def arrow(x1, y1, x2, y2, color="#5F6368", dash=None, both=False, sw=2.5):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    ms = ' marker-start="url(#arr)"' if both else ""
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{sw}"{d} marker-end="url(#arr)"{ms}/>')


def badge(cx, cy, n, color="#202124"):
    out.append(f'<circle cx="{cx}" cy="{cy}" r="15" fill="{color}"/>')
    text(cx, cy + 1, str(n), size=15, weight="bold", color="#FFFFFF")


def label(cx, cy, lines, w, size=13.5):
    h = 16 + 20 * len(lines)
    rect(cx - w / 2, cy - h / 2, w, h, "#FFFFFF", "#DADCE0", rx=6, sw=1.2)
    text(cx, cy, lines, size=size, lh=20)


def component(x, y, w, h, title, subtitle, items, stroke, fill="#FFFFFF"):
    rect(x, y, w, h, fill, stroke, rx=14, sw=2.5)
    text(x + w / 2, y + 30, title, size=19, weight="bold", color=stroke)
    text(x + w / 2, y + 56, subtitle, size=13.5, color="#5F6368")
    out.append(f'<line x1="{x + 18}" y1="{y + 76}" x2="{x + w - 18}" y2="{y + 76}" stroke="#E0E3E7" stroke-width="1.5"/>')
    for i, item in enumerate(items):
        text(x + 22, y + 102 + i * 27, "✓  " + item, size=14, anchor="start")


out.append(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
    '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    '<path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker></defs>'
    f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>'
)

# Title
text(W / 2, 38, "Identité utilisateur de bout en bout : myID (Keycloak) → BFF → Agent Foundry → MCP", size=22, weight="bold", color="#1F3864")

# Identity providers (top)
rect(40, 80, 900, 96, "#FFF4E5", "#E8710A", rx=14, sw=2.5)
text(490, 110, "myID = Keycloak · realm « weather »", size=19, weight="bold", color="#B45309")
text(490, 145, "Clients : weather-mobile (public, PKCE) · weather-bff (confidentiel, token exchange) · weather-mcp (audience) · JWKS", size=13.5, color="#5F2B00")
rect(1010, 80, 420, 96, "#E6F4EA", "#188038", rx=14, sw=2.5)
text(1220, 110, "Microsoft Entra ID", size=19, weight="bold", color="#188038")
text(1220, 145, "identité managée dédiée du BFF uniquement", size=13.5, color="#0D3B1E")

# Components (main chain)
CY, CH = 330, 300
comps = [
    (40, 290, "App web / mobile", "client public weather-mobile", ["Login OIDC + PKCE", "Jeton A uniquement", "Aucun secret embarqué"], "#D93025"),
    (400, 380, "BFF · API « chat »", "API Management (apim) ou Python (code)", [
        "Refus des en-têtes d'identité",
        "Validation de A (aud, azp, exp)",
        "Échange A → B (OBO) + cache",
        "Validation de B (aud, azp, sub)",
        "Corps en liste blanche, CORS",
        "Session dérivée de l'identité",
        "Limites par IP et par utilisateur",
    ], "#188038"),
    (850, 380, "Agent hébergé Foundry", "Agent Framework · weather-agent", [
        "Passerelle : RBAC + impersonation",
        "Conversations isolées par utilisateur",
        "Pré-contrôle de B (sinon 401)",
        "B lié à la requête, jamais au modèle",
        "Outils MCP définis dans le code",
    ], "#1A73E8"),
    (1300, 360, "Serveur MCP météo", "resource server weather-mcp", [
        "Valide B (signature, iss, aud)",
        "azp = weather-bff (sinon 401)",
        "Scope vérifié par outil",
        "Utilisateur = claim sub",
    ], "#8430CE"),
]
for x, w, title, sub, items, color in comps:
    component(x, CY, w, CH, title, sub, items, color)

# Chain arrows
ay = CY + 40
arrow(330, ay, 400, ay, color="#1A73E8", sw=3)
badge(365, ay - 26, 2)
arrow(780, ay, 850, ay, color="#188038", sw=3)
badge(815, ay - 26, 5)
arrow(1230, ay, 1300, ay, color="#8430CE", sw=3)
badge(1265, ay - 26, 6)

# Identity provider arrows
arrow(185, CY, 185, 176, color="#E8710A", both=True, sw=3)
badge(185, 253, 1, "#E8710A")
arrow(520, CY, 520, 176, color="#8430CE", both=True, sw=3)
badge(520, 253, 3, "#8430CE")
arrow(700, CY, 1060, 176, color="#188038", both=True, sw=3)
badge(880, 253, 4, "#188038")

# Legend (numbered steps)
LY = 665
rect(40, LY, 1620, 170, "#F8F9FA", "#DADCE0", rx=12, sw=1.2)
steps = [
    ("1", "#E8710A", "Login myID (PKCE) → jeton A : aud = weather-bff, azp = weather-mobile"),
    ("2", "#1A73E8", "App → BFF : Authorization: Bearer A"),
    ("3", "#8430CE", "Le BFF échange A → B auprès de myID : aud = weather-mcp, azp = weather-bff, même sub"),
    ("4", "#188038", "Le BFF obtient le jeton Entra E de son identité managée dédiée"),
    ("5", "#188038", "BFF → Agent : Bearer E · x-ms-user-identity · x-client-mcp-token: B · session dérivée"),
    ("6", "#8430CE", "Agent → MCP : Authorization: Bearer B"),
]
for i, (n, color, desc) in enumerate(steps):
    col, row = i % 2, i // 2
    x = 70 + col * 800
    y = LY + 35 + row * 48
    badge(x, y, n, color)
    text(x + 28, y + 1, desc, size=14.5, anchor="start")

out.append("</svg>")
Path(__file__).with_name("architecture.svg").write_text("".join(out), encoding="utf-8")
print("architecture.svg written")
