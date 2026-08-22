"""Audit correctif Mission 7, §9 — le garde-fou SSRF (services/url_safety.py)
fait une vraie résolution DNS pour décider si une URL est sûre. Les tests de
ce paquet inventent des domaines fictifs (agence-exemple.example,
exemple.fr...) pour simuler des sites web sans jamais faire de vraie requête
réseau : sans ce correctif, TOUT test de ce genre échouerait le garde-fou
(DNS introuvable -> "non sûr") et casserait silencieusement (aucune requête
HTTP n'aurait lieu), alors que la sécurité de PRODUCTION (résolution réelle
pour un vrai hostname) reste entièrement inchangée — seul le process de test
a ce resolveur DNS patché, jamais le code applicatif lui-même.

Ne tente JAMAIS de vraie résolution DNS (tests hors-ligne, déterministes,
rapides) : tout hostname (chaîne non-IP) est traité comme public par défaut
pendant les tests. Une IP littérale (ex. "10.0.0.5" dans un test SSRF) est
renvoyée telle quelle, sans jamais être remplacée — indispensable pour que
les tests qui vérifient le rejet d'IP privées/loopback restent valides. Un
test qui veut vérifier le rejet d'un HOSTNAME résolvant vers une IP privée
patche explicitement `socket.getaddrinfo` lui-même
(voir test_mission7_audit_correctif.py) — ce patch local prend le pas sur
celui-ci le temps du test."""
import ipaddress
import socket

_FAKE_PUBLIC_IP = "93.184.216.34"  # IP historique d'example.com — publique, stable.


def _test_safe_getaddrinfo(host, *args, **kwargs):
    try:
        ipaddress.ip_address(host)
        resolved = host
    except (ValueError, TypeError):
        resolved = _FAKE_PUBLIC_IP
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, 0))]


socket.getaddrinfo = _test_safe_getaddrinfo
