"""ETAPE 26 (mission 2) — diagnostic de délivrabilité, jamais de modification DNS.

Constate l'état actuel (SPF/DMARC/MX) sans jamais affirmer qu'un DKIM fonctionne :
DKIM dépend d'un sélecteur propre à l'expéditeur, non déterminable automatiquement.
"""
import dns.resolver


def _txt_records(domain, timeout=4):
    try:
        answers = dns.resolver.resolve(domain, "TXT", lifetime=timeout)
        return [b"".join(r.strings).decode("utf-8", "ignore") if hasattr(r, "strings") else str(r) for r in answers]
    except Exception:
        return None  # None = non déterminable (ne pas confondre avec "absent")


def diagnose_domain(domain, dkim_selector=""):
    domain = (domain or "").strip().lower()
    if not domain:
        return {"domain": "", "smtp_configured": False}

    result = {"domain": domain}

    txt_records = _txt_records(domain)
    if txt_records is None:
        result["spf_detected"] = "inconnu"
    else:
        result["spf_detected"] = "oui" if any(r.lower().startswith("v=spf1") for r in txt_records) else "non"

    dmarc_records = _txt_records(f"_dmarc.{domain}")
    if dmarc_records is None:
        result["dmarc_detected"] = "inconnu"
    else:
        result["dmarc_detected"] = "oui" if any("v=dmarc1" in r.lower() for r in dmarc_records) else "non"

    try:
        mx = dns.resolver.resolve(domain, "MX", lifetime=4)
        result["mx_present"] = "présent" if len(mx) > 0 else "absent"
    except Exception:
        result["mx_present"] = "inconnu"

    if dkim_selector:
        dkim_records = _txt_records(f"{dkim_selector}._domainkey.{domain}")
        result["dkim_status"] = "configuré (sélecteur trouvé)" if dkim_records else "non vérifiable avec ce sélecteur"
    else:
        result["dkim_status"] = "sélecteur non configuré — non vérifiable"

    return result
