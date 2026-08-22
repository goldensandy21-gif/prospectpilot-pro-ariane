"""Audit correctif Mission 7, §9 — garde-fou SSRF central, réutilisé par
crawler.py (pages + sitemap), quick_scan.py et robots.py. Aucune requête vers
une adresse privée/loopback/link-local/réservée ; toute redirection est
revalidée avant d'être suivie ; taille de réponse plafonnée."""
import ipaddress
import socket
from urllib.parse import urlparse

MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 Mo, largement suffisant pour une page HTML/XML.
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


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
    Lève UnsafeUrlError si une étape n'est pas sûre ou si la réponse est trop
    grosse — l'appelant doit alors ignorer cette page, jamais la traiter."""
    for step in list(getattr(response, "history", []) or []) + [response]:
        if not is_safe_url(str(step.url)):
            raise UnsafeUrlError(str(step.url))
    content_length = len(response.content or b"")
    if content_length > MAX_RESPONSE_BYTES:
        raise UnsafeUrlError(f"réponse trop volumineuse ({content_length} octets)")
