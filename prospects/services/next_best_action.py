"""Mission 6, section 12 — Next Best Action structurée.

Remplace le simple dict grade -> phrase de agent_brief.py par un service
déterministe qui renvoie un code (WAIT / WATCH / LINKEDIN_CONNECT /
LINKEDIN_MESSAGE / EMAIL / FOLLOW_UP / STOP / NURTURE), une raison, une
confiance et le signal déclencheur. Écrit toujours dans le champ existant
`AgentBrief.next_best_action` (voir agent_brief.py) — aucun second système
de recommandation, aucune nouvelle table.
"""
from ..models import ContactLog, Suppression
from .linkedin_orchestration import linkedin_profile_url
from .signal_freshness import signal_freshness

STOP_OUTCOMES = {"optout", "won", "lost"}
FOLLOW_UP_OUTCOMES = {"replied", "meeting", "proposal"}
RECENT_CONTACT_GRACE_DAYS = 3
HIGH_INTENT_THRESHOLD = 60
EMERGING_INTENT_THRESHOLD = 30
NURTURE_FIT_THRESHOLD = 50


def _is_excluded(prospect):
    if prospect.status == "do_not_contact":
        return "Prospect marqué « Ne plus contacter »."
    if prospect.predictneed_excluded:
        return prospect.predictneed_exclusion_reason or "Prospect exclu de la prospection."
    if Suppression.objects.filter(active=True, prospect=prospect).exists():
        return "Prospect présent dans la liste d'opposition."
    if prospect.public_email and Suppression.objects.filter(active=True, email__iexact=prospect.public_email).exists():
        return "Adresse e-mail en liste d'opposition."
    return ""


def _best_email(prospect):
    return prospect.public_emails.filter(is_active=True).order_by("-is_primary", "-confidence_score").first()


def _most_recent_signal(prospect, signal_group=None):
    qs = prospect.signals.all()
    if signal_group:
        qs = qs.filter(signal_group=signal_group)
    return qs.order_by("-observed_at", "-detected_at").first()


def _triggering_signal_text(signal, now=None):
    if not signal:
        return ""
    freshness = signal_freshness(signal.observed_at or signal.detected_at, now=now)
    age = f"{freshness['age_days']:.0f}" if freshness["age_days"] is not None else "?"
    return f"{signal.label} détecté il y a {age} jour(s)"


def compute_next_best_action(prospect, now=None):
    exclusion_reason = _is_excluded(prospect)
    if exclusion_reason:
        return {"code": "STOP", "reason": exclusion_reason, "confidence": 100, "triggering_signal": ""}

    last_log = prospect.contact_logs.order_by("-contacted_at").first()

    if last_log and last_log.outcome in FOLLOW_UP_OUTCOMES:
        return {
            "code": "FOLLOW_UP",
            "reason": f"Le prospect a interagi ({last_log.get_outcome_display()}) — un suivi humain est nécessaire.",
            "confidence": 90,
            "triggering_signal": "",
        }

    if last_log and last_log.outcome in STOP_OUTCOMES:
        return {
            "code": "STOP",
            "reason": f"Dernier contact classé « {last_log.get_outcome_display()} » : ne plus solliciter.",
            "confidence": 90,
            "triggering_signal": "",
        }

    if last_log:
        age_days = signal_freshness(last_log.contacted_at, now=now)["age_days"]
        if age_days is not None and age_days < RECENT_CONTACT_GRACE_DAYS:
            return {
                "code": "WAIT",
                "reason": f"Déjà contacté il y a {age_days:.0f} jour(s) : laisser le temps de répondre avant de resolliciter.",
                "confidence": 70,
                "triggering_signal": "",
            }

    intent_score = prospect.intent_score
    has_email = _best_email(prospect) is not None
    has_linkedin = bool(linkedin_profile_url(prospect))
    contacted_linkedin_before = prospect.contact_logs.filter(channel="linkedin").exists()
    contacted_email_before = prospect.contact_logs.filter(channel="email").exists()

    if intent_score >= HIGH_INTENT_THRESHOLD:
        trigger = _most_recent_signal(prospect, signal_group="intent")
        trigger_text = _triggering_signal_text(trigger, now=now)
        contact_note = "aucun contact antérieur" if not last_log else "après un précédent contact"

        if has_linkedin and not contacted_linkedin_before:
            return {
                "code": "LINKEDIN_CONNECT",
                "reason": f"Intent {intent_score}, {trigger_text}, {contact_note}." if trigger_text else f"Intent {intent_score}, {contact_note}.",
                "confidence": 80,
                "triggering_signal": trigger.label if trigger else "",
            }
        if has_linkedin and contacted_linkedin_before:
            return {
                "code": "LINKEDIN_MESSAGE",
                "reason": f"Intent {intent_score}, déjà en relation LinkedIn : passer au message.",
                "confidence": 75,
                "triggering_signal": trigger.label if trigger else "",
            }
        if has_email and not contacted_email_before:
            return {
                "code": "EMAIL",
                "reason": f"Intent {intent_score}, {trigger_text}, {contact_note}." if trigger_text else f"Intent {intent_score}, {contact_note}.",
                "confidence": 75,
                "triggering_signal": trigger.label if trigger else "",
            }
        return {
            "code": "WATCH",
            "reason": f"Intent {intent_score} mais aucun canal de contact exploitable (ni e-mail, ni LinkedIn).",
            "confidence": 60,
            "triggering_signal": trigger.label if trigger else "",
        }

    if intent_score >= EMERGING_INTENT_THRESHOLD:
        trigger = _most_recent_signal(prospect, signal_group="intent")
        return {
            "code": "WATCH",
            "reason": f"Intent {intent_score} : signaux émergents, à surveiller avant un contact direct.",
            "confidence": 55,
            "triggering_signal": trigger.label if trigger else "",
        }

    if prospect.icp_fit_score >= NURTURE_FIT_THRESHOLD:
        return {
            "code": "NURTURE",
            "reason": f"Bon fit ({prospect.icp_fit_score}/100) mais aucune intention détectée pour le moment : nurturing long terme.",
            "confidence": 50,
            "triggering_signal": "",
        }

    return {
        "code": "WAIT",
        "reason": "Aucun signal d'intention et fit limité : ne pas prioriser pour le moment.",
        "confidence": 40,
        "triggering_signal": "",
    }
