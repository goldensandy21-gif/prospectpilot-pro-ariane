"""ETAPE 30/16 (mission 4) — timeline commerciale d'un prospect.

Construite uniquement à partir d'événements réellement enregistrés
(EngagementEvent, EmailSend, ConversionEvent, CrawlRun, PublicEmail...) —
jamais une étape fictive ou déduite.

Mission 6, section 13 : étend cette même timeline (signal détecté,
invitation/message LinkedIn, recalcul de score, NBA importante) plutôt que
de créer un second modèle d'événements — toujours à partir de lignes
réellement enregistrées (ProspectSignal.observed_at, ContactLog, AgentBrief),
jamais une étape déduite.
"""
LINKEDIN_OUTCOME_LABELS = {
    "invitation_prepared": "Invitation LinkedIn préparée",
    "invitation_sent": "Invitation LinkedIn envoyée",
    "invitation_accepted": "Invitation LinkedIn acceptée",
    "invitation_declined": "Invitation LinkedIn refusée ou expirée",
    "message_prepared": "Message LinkedIn préparé",
    "sent": "Message LinkedIn envoyé",
    "replied": "Réponse LinkedIn reçue",
}

# NBA jugées "importantes" pour la timeline (mission 6, section 13) : on
# n'affiche pas WAIT/WATCH/NURTURE à chaque recalcul, uniquement les actions
# concrètes recommandées, pour ne pas noyer la timeline.
NOTABLE_NBA_CODES = {"LINKEDIN_CONNECT", "LINKEDIN_MESSAGE", "EMAIL", "FOLLOW_UP", "STOP"}

EVENT_LABELS = {
    "email_sent": "E-mail envoyé",
    "email_failed": "Échec d'envoi e-mail",
    "link_clicked": "Lien cliqué",
    "product_visited": "Visite PredictNeed",
    "simulator_started": "Simulateur démarré",
    "simulator_completed": "Simulateur terminé",
    "signup_started": "Inscription démarrée",
    "signup_completed": "Inscription terminée",
    "checkout_started": "Paiement démarré",
    "subscription_activated": "Abonnement activé",
    "subscription_cancelled": "Abonnement annulé",
}


def build_prospect_timeline(prospect):
    entries = []

    if prospect.created_at:
        entries.append({
            "at": prospect.created_at, "label": "Prospect identifié",
            "meta": prospect.source or "", "predictneed": False,
        })

    candidate = prospect.search_candidate_origins.order_by("created_at").first()
    if candidate and candidate.site_url:
        entries.append({
            "at": candidate.updated_at, "label": "Site officiel découvert",
            "meta": f"confiance {candidate.site_confidence}/100" if candidate.site_confidence else "",
            "predictneed": False,
        })

    latest_crawl = prospect.crawl_runs.filter(status="done", finished_at__isnull=False).order_by("-finished_at").first()
    if latest_crawl:
        entries.append({
            "at": latest_crawl.finished_at, "label": "Site analysé",
            "meta": f"{latest_crawl.pages_crawled} page(s) analysée(s)", "predictneed": False,
        })

    best_email = prospect.public_emails.filter(is_active=True).order_by("found_at").first()
    if best_email:
        entries.append({
            "at": best_email.found_at, "label": "Contact trouvé",
            "meta": best_email.email, "predictneed": False,
        })

    for send in prospect.email_sends.filter(status="sent", is_test=False):
        if send.sent_at:
            entries.append({
                "at": send.sent_at, "label": "E-mail envoyé",
                "meta": send.subject, "predictneed": False,
            })

    for event in prospect.engagement_events.all():
        entries.append({
            "at": event.occurred_at,
            "label": EVENT_LABELS.get(event.event_type, event.event_type),
            "meta": "", "predictneed": event.source == "predictneed",
        })

    for conversion in prospect.conversion_events.all():
        entries.append({
            "at": conversion.occurred_at,
            "label": conversion.get_event_type_display(),
            "meta": conversion.external_reference, "predictneed": True,
        })

    for signal in prospect.signals.all():
        entries.append({
            "at": signal.observed_at or signal.detected_at,
            "label": f"Signal détecté : {signal.label}",
            "meta": signal.get_signal_group_display(), "predictneed": False,
        })

    for log in prospect.contact_logs.filter(channel="linkedin"):
        entries.append({
            "at": log.contacted_at,
            "label": LINKEDIN_OUTCOME_LABELS.get(log.outcome, f"LinkedIn : {log.get_outcome_display()}"),
            "meta": log.subject, "predictneed": False,
        })

    if prospect.scores_computed_at:
        entries.append({
            "at": prospect.scores_computed_at,
            "label": "Scores INTENT/ENGAGEMENT recalculés",
            "meta": f"Intent {prospect.intent_score}, Engagement {prospect.engagement_score}",
            "predictneed": False,
        })

    latest_brief = prospect.agent_briefs.order_by("-generated_at").first()
    if latest_brief and latest_brief.next_best_action:
        code = latest_brief.next_best_action.split(" — ", 1)[0]
        if code in NOTABLE_NBA_CODES:
            entries.append({
                "at": latest_brief.generated_at,
                "label": f"Action recommandée : {code}",
                "meta": latest_brief.next_best_action, "predictneed": False,
            })

    entries = [e for e in entries if e["at"] is not None]
    entries.sort(key=lambda e: e["at"])
    return entries
