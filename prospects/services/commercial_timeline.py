"""ETAPE 30/16 (mission 4) — timeline commerciale d'un prospect.

Construite uniquement à partir d'événements réellement enregistrés
(EngagementEvent, EmailSend, ConversionEvent, CrawlRun, PublicEmail...) —
jamais une étape fictive ou déduite.
"""
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

    entries = [e for e in entries if e["at"] is not None]
    entries.sort(key=lambda e: e["at"])
    return entries
