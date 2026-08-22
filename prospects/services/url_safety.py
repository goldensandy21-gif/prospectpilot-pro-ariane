"""Garde-fou SSRF central, réutilisé par crawler.py (pages/sitemap/liens),
quick_scan.py, robots.py et site_discovery.py.

Audit correctif round 2, §2 — `safe_get()` est le seul point d'entrée réseau
recommandé : il valide l'URL AVANT chaque requête (initiale, puis CHAQUE
étape de redirection), ne laisse jamais httpx suivre une redirection tout
seul (`follow_redirects=True` interrogerait la cible avant toute
validation), et lit le corps en flux avec un plafond de taille appliqué
PENDANT la lecture (jamais un téléchargement intégral suivi d'une
vérification a posteriori).

LIMITE DE SÉCURITÉ CONNUE (documentée honnêtement, jamais présentée comme
résolue) — DNS rebinding : `is_safe_url()` fait sa propre résolution DNS
pour décider si une URL est sûre, puis httpx fait sa PROPRE résolution
indépendante quelques instants plus tard pour établir la connexion TCP réelle.
Un attaquant contrôlant son propre serveur DNS peut faire pointer le même
hostname vers une IP publique au moment du premier lookup puis vers une IP
privée au moment de la connexion réelle (TOCTOU). Ce module NE PIN PAS la
connexion sur l'IP validée (cela demanderait un transport HTTP personnalisé
tout en préservant SNI/Host correctement — hors périmètre de ce correctif de
stabilisation). Pour une protection complète en production, ajouter une
règle réseau de sortie (egress) bloquant les plages privées/loopback/
link-local au niveau infrastructure (proxy sortant, règles firewall/VPC),
en complément de ce garde-fou applicatif — jamais en remplacement."""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 Mo, largement suffisant pour une page HTML/XML.
DEFAULT_MAX_REDIRECTS = 5
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class UnsafeUrlError(Exception):
    """Levée quand une URL (ou une étape de redirection) échoue au garde-fou SSRF."""


def _is_public_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def is_safe_url(url):
    """True seulement si l'URL est http(s), a un hostname, et TOUTES les IP
    vers lesquelles ce hostname résout sont publiques. Ne lève jamais —
    toute erreur (parsing, DNS indisponible) est traitée comme non sûre."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTNAMES:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    ips = {info[4][0] for info in infos}
    if not ips:
        return False
    return all(_is_public_ip(ip) for ip in ips)


def assert_safe_response(response):
    """Revalide chaque étape de la chaîne de redirection (response.history,
    httpx) plus l'URL finale, et plafonne la taille du contenu déjà reçu.
    Conservé pour compatibilité/tests directs — les appelants de production
    utilisent désormais `safe_get()`, qui valide chaque redirection AVANT de
    la requêter plutôt qu'après."""
    for step in list(getattr(response, "history", []) or []) + [response]:
        if not is_safe_url(str(step.url)):
            raise UnsafeUrlError(str(step.url))
    content_length = len(response.content or b"")
    if content_length > MAX_RESPONSE_BYTES:
        raise UnsafeUrlError(f"réponse trop volumineuse ({content_length} octets)")


class SafeResponse:
    """Enveloppe minimale compatible avec le sous-ensemble de l'API
    httpx.Response utilisé dans ce repo (.status_code, .headers, .content,
    .text, .url, .history). `.history` est toujours vide : safe_get() a déjà
    validé chaque saut avant de le suivre, il n'y a rien à revalider après coup."""

    def __init__(self, status_code, headers, content, url):
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.url = url
        self.history = []

    @property
    def text(self):
        encoding = "utf-8"
        content_type = self.headers.get("content-type", "") if self.headers else ""
        if "charset=" in content_type:
            encoding = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def safe_get(client, url, method="GET", max_redirects=DEFAULT_MAX_REDIRECTS, max_bytes=MAX_RESPONSE_BYTES, **stream_kwargs):
    """Requête sûre contre le SSRF — point d'entrée réseau unique recommandé.

    - Valide l'URL AVANT chaque requête, y compris chaque étape de
      redirection (jamais après, contrairement à follow_redirects=True).
    - Ne laisse jamais httpx suivre une redirection lui-même : `client` doit
      être construit sans `follow_redirects=True` (valeur par défaut httpx).
    - Lit le corps en flux (`client.stream`) avec un plafond appliqué PENDANT
      la lecture (Content-Length vérifié quand présent, mais jamais fait
      confiance seul : la taille réelle lue est toujours comptée).
    - Lève UnsafeUrlError si une étape est refusée, si une redirection n'a
      pas de Location, ou si le flux dépasse max_bytes — jamais un
      téléchargement complet suivi d'une vérification a posteriori."""
    current_url = url
    for _ in range(max_redirects + 1):
        if not is_safe_url(current_url):
            raise UnsafeUrlError(current_url)
        with client.stream(method, current_url, **stream_kwargs) as response:
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location", "")
                if not location:
                    raise UnsafeUrlError(f"redirection sans Location depuis {current_url}")
                next_url = urljoin(current_url, location)
                if not is_safe_url(next_url):
                    raise UnsafeUrlError(next_url)
                current_url = next_url
                continue

            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                raise UnsafeUrlError(f"Content-Length {content_length} > {max_bytes}")

            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise UnsafeUrlError(f"réponse trop volumineuse (> {max_bytes} octets)")
            return SafeResponse(response.status_code, dict(response.headers), bytes(chunks), current_url)

    raise UnsafeUrlError(f"trop de redirections depuis {url}")
