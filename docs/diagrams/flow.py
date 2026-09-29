"""Generates flow.svg: authentication data-flow diagram with trust zones."""

from html import escape
from pathlib import Path

W, H = 2100, 1080
FONT = "Arial, Helvetica, sans-serif"

TOK = {  # token colour coding
    "A": ("#1A73E8", "#E8F0FE"),
    "B": ("#8430CE", "#F3E8FD"),
    "E": ("#188038", "#E6F4EA"),
}

out: list[str] = []


def text(x, y, lines, size=15, weight="normal", color="#202124", anchor="middle", lh=None):
    lh = lh or size * 1.3
    if isinstance(lines, str):
        lines = [lines]
    y0 = y - (len(lines) - 1) * lh / 2
    spans = "".join(
        f'<tspan x="{x}" y="{y0 + i * lh:.1f}">{escape(l)}</tspan>' for i, l in enumerate(lines)
    )
    out.append(
        f'<text font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{color}" '
        f'text-anchor="{anchor}" dominant-baseline="middle">{spans}</text>'
    )


def rect(x, y, w, h, fill, stroke, rx=10, dash=None, sw=2):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')


def box(cx, cy, w, h, title, sub, fill, stroke):
    rect(cx - w / 2, cy - h / 2, w, h, fill, stroke, rx=12, sw=2.5)
    text(cx, cy - h / 2 + 26, title, size=18, weight="bold", color=stroke)
    text(cx, cy + 14, sub, size=13.5, color="#3C4043")


def arrow(x1, y1, x2, y2, color="#5F6368", dash=None, both=False, sw=2.5):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    ms = ' marker-start="url(#arrS)"' if both else ""
    out.append(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{sw}"{d}'
        f' marker-end="url(#arr)"{ms}/>'
    )


def badge(cx, cy, n, color="#202124"):
    out.append(f'<circle cx="{cx}" cy="{cy}" r="14" fill="{color}"/>')
    text(cx, cy + 1, str(n), size=14, weight="bold", color="#FFFFFF")


def pill(x, y, tok, label, anchor="start"):
    stroke, fill = TOK[tok]
    w = 12 + len(label) * 7.1
    x0 = x if anchor == "start" else x - w / 2
    rect(x0, y - 13, w, 26, fill, stroke, rx=13, sw=1.6)
    text(x0 + w / 2, y + 1, label, size=13, weight="bold", color=stroke)
    return w


def label(cx, cy, lines, w, h=None):
    h = h or 18 + 19 * len(lines)
    rect(cx - w / 2, cy - h / 2, w, h, "#FFFFFF", "#DADCE0", rx=6, sw=1.2)
    text(cx, cy, lines, size=13, color="#202124", lh=19)


# ---------------------------------------------------------------- layout
COLS = {"app": 190, "bff": 610, "gw": 1030, "agent": 1450, "mcp": 1890}
BOX_W, BOX_H, CHAIN_Y = 232, 120, 560
IDP_Y, ENTRA_Y = 110, 330

out.append(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
    '<defs>'
    '<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    '<path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker>'
    '<marker id="arrS" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    '<path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker>'
    '</defs>'
    f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>'
)

# Trust zones (background)
zones = [
    ("app", "Zone utilisateur", "non fiable", "#FCE8E6", "#D93025", 40, 360),
    ("bff", "API Management", "de confiance", "#E6F4EA", "#188038", 440, 340),
    ("foundry", "Microsoft Foundry", "plateforme managée", "#E8F0FE", "#1A73E8", 830, 830),
    ("mcp", "Ressource protégée", "resource server", "#F3E8FD", "#8430CE", 1710, 360),
]
for _, name, sub, fill, stroke, x, w in zones:
    rect(x, 250, w, 640, fill, stroke, rx=16, dash="8 6", sw=1.6)
    text(x + w / 2, 866, f"{name}  ·  {sub}", size=15, weight="bold", color=stroke)

# IdP band
rect(40, IDP_Y - 55, 2030, 110, "#FFF4E5", "#E8710A", rx=14, sw=2.5)
text(1055, IDP_Y - 18, "myID = Keycloak · realm « weather » · émetteur unique", size=20, weight="bold", color="#B45309")
text(1055, IDP_Y + 18, "Clients : weather-mobile (public, PKCE) · weather-bff (confidentiel, Standard Token Exchange RFC 8693) · weather-mcp (audience) · JWKS", size=14, color="#5F2B00")

# Entra
rect(COLS["gw"] - 135, ENTRA_Y - 34, 270, 68, "#FFFFFF", "#188038", rx=10, sw=2.5)
text(COLS["gw"], ENTRA_Y - 10, "Microsoft Entra ID", size=16, weight="bold", color="#188038")
text(COLS["gw"], ENTRA_Y + 14, "identité dédiée d'APIM uniquement", size=13, color="#3C4043")

# Main chain
box(COLS["app"], CHAIN_Y, BOX_W, BOX_H, "App mobile / web", ["Client public weather-mobile", "PKCE · jeton A uniquement"], "#FFFFFF", "#D93025")
box(COLS["bff"], CHAIN_Y, BOX_W, BOX_H, "APIM · API chat", ["BFF sans code (policies)", "Client confidentiel weather-bff"], "#FFFFFF", "#188038")
box(COLS["gw"], CHAIN_Y, BOX_W, BOX_H, "Gateway Foundry", ["RBAC + impersonation", "Isolation par utilisateur"], "#FFFFFF", "#1A73E8")
box(COLS["agent"], CHAIN_Y, BOX_W, BOX_H, "Agent hébergé", ["Agent Framework", "Jeton lié à la requête"], "#FFFFFF", "#1A73E8")
box(COLS["mcp"], CHAIN_Y, BOX_W, BOX_H, "Serveur MCP", ["Resource server weather-mcp", "Identité = claim sub"], "#FFFFFF", "#8430CE")

half = BOX_W / 2
# Chain arrows + labels
arrow(COLS["app"] + half, CHAIN_Y, COLS["bff"] - half, CHAIN_Y, color="#1A73E8")
badge(COLS["app"] + half + 30, CHAIN_Y - 22, 2)
label((COLS["app"] + COLS["bff"]) / 2, CHAIN_Y + 50, ["Authorization: Bearer A"], 168)

arrow(COLS["bff"] + half, CHAIN_Y, COLS["gw"] - half, CHAIN_Y, color="#188038")
badge(COLS["bff"] + half + 30, CHAIN_Y - 22, 5)
label((COLS["bff"] + COLS["gw"]) / 2, CHAIN_Y + 78, ["Bearer E", "x-ms-user-identity:", "hash(iss|sub)", "x-client-mcp-token: B"], 168)

arrow(COLS["gw"] + half, CHAIN_Y, COLS["agent"] - half, CHAIN_Y, color="#8430CE")
badge(COLS["gw"] + half + 30, CHAIN_Y - 22, 6)
label((COLS["gw"] + COLS["agent"]) / 2, CHAIN_Y + 62, ["Authorization retiré", "x-client-* transmis"], 168)

arrow(COLS["agent"] + half, CHAIN_Y, COLS["mcp"] - half, CHAIN_Y, color="#8430CE")
badge(COLS["agent"] + half + 30, CHAIN_Y - 22, 7)
label((COLS["agent"] + COLS["mcp"]) / 2, CHAIN_Y + 50, ["Authorization: Bearer B"], 180)

# App <-> IdP (login + tokens)
top = CHAIN_Y - BOX_H / 2
arrow(COLS["app"] - 40, top, COLS["app"] - 40, IDP_Y + 55, color="#E8710A", both=True)
badge(COLS["app"] - 40, (top + IDP_Y + 55) / 2 - 60, 1, "#E8710A")
label(COLS["app"] + 10, (top + IDP_Y + 55) / 2 + 20, ["Authorization Code + PKCE", "→ jeton A", "aud=weather-bff", "azp=weather-mobile", "+ refresh token rotatif"], 190)

# BFF <-> IdP (JWKS + OBO) and BFF -> Entra
arrow(COLS["bff"] - 60, top, COLS["bff"] - 60, IDP_Y + 55, color="#8430CE", both=True, sw=3)
badge(COLS["bff"] - 60, 205, 3, "#8430CE")
label(COLS["bff"] - 60, 300, ["Token exchange (OBO)", "subject_token = A", "audience = weather-mcp", "→ jeton B", "(+ JWKS)"], 180)
arrow(COLS["bff"] + 70, top, COLS["gw"] - 135, ENTRA_Y + 22, color="#188038", both=True)
badge(COLS["bff"] + 165, 430, 4, "#188038")
label(COLS["bff"] + 262, 462, ["jeton E", "(managed identity)"], 140)
arrow(COLS["gw"], top, COLS["gw"], ENTRA_Y + 34, color="#188038", dash="3 5", sw=2)
label(COLS["gw"], 440, ["valide E"], 90)

# Agent / MCP -> IdP (JWKS)
arrow(COLS["agent"], top, COLS["agent"], IDP_Y + 55, color="#E8710A", dash="3 5", sw=2)
label(COLS["agent"], 330, ["JWKS", "(pré-contrôle)"], 120)
arrow(COLS["mcp"], top, COLS["mcp"], IDP_Y + 55, color="#E8710A", dash="3 5", sw=2)
label(COLS["mcp"], 330, ["JWKS", "(validation)"], 120)

# Checks under each component
checks = {
    "app": ["Aucun secret embarqué", "Jamais de jeton pour le MCP", "Refresh token rotatif"],
    "bff": ["En-têtes d'identité refusés", "A : aud, azp=weather-mobile", "B : même sub, azp=weather-bff", "Corps et session imposés"],
    "gw": ["Valide E (Entra)", "Rôle Agent Consumer", "Droit UserIdentityImpersonation", "Conversations isolées"],
    "agent": ["B vérifié (aud=weather-mcp, exp)", "Sinon 401 (fail closed)", "Jamais dans prompt / logs", "Un jeton par requête"],
    "mcp": ["Signature, iss, aud=weather-mcp", "azp = weather-bff (sinon 401)", "Scope contrôlé par outil", "Utilisateur = claim sub"],
}
for key, lines in checks.items():
    cx = COLS[key]
    y0 = 720
    h = 36 + 22 * len(lines)
    rect(cx - 135, y0, 270, h, "#FFFFFF", "#9AA0A6", rx=8, dash="5 4", sw=1.4)
    text(cx, y0 + 16, "Contrôles", size=13, weight="bold", color="#5F6368")
    for i, l in enumerate(lines):
        text(cx - 118, y0 + 40 + i * 22, "✓  " + l, size=13.5, anchor="start")

# Legend
ly = 960
text(60, ly - 36, "Jetons", size=16, weight="bold", anchor="start")
x = 60
x += pill(x, ly, "A", "A · jeton utilisateur · aud = weather-bff · azp = weather-mobile · 5 min") + 24
x += pill(x, ly, "B", "B · jeton utilisateur (OBO) · aud = weather-mcp · azp = weather-bff · 5 min") + 24
pill(x, ly, "E", "E · jeton Entra (identité APIM dédiée) · aud = Foundry")
text(60, ly + 44, "Le même utilisateur (sub) est porté par A et B. Seul APIM (le BFF) peut obtenir B (échange de jeton) ; Foundry voit l'identité déléguée x-ms-user-identity ; seul le MCP consomme B.",
     size=14, color="#3C4043", anchor="start")

out.append("</svg>")
Path(__file__).with_name("flow.svg").write_text("".join(out), encoding="utf-8")
print("flow.svg written")
