"""Mission 6, section 17 — analytics par signal/canal/bande d'Intent.

Réutilise exclusivement les événements et modèles déjà existants
(ProspectSignal, ContactLog, EngagementEvent, ConversionEvent,
RevenueAttribution, EmailStep.channel du bloc C, IN_MARKET_LEVELS du bloc
IN MARKET NOW) — aucune nouvelle table de mesure, aucune deuxième formule de
bande d'Intent. Pas de pseudo-IA qui réajuste des pondérations : uniquement
des comptages/taux robustes sur l'historique réel, base pour une
optimisation future construite sur des données réelles (mission 6,
section 17 — explicitement pas maintenant).
"""
from django.db.models import Count, Sum

from ..models import ConversionEvent, ContactLog, EngagementEvent, Prospect, ProspectSignal, RevenueAttribution
from .in_market_status import IN_MARKET_LEVELS


def _prospect_ids(queryset):
    return set(queryset.values_list("prospect_id", flat=True))


def _signal_type_counts_for(prospect_ids):
    """Nombre de prospects distincts par signal_type, parmi `prospect_ids` —
    fonction générique réutilisée par signal->réponse/clic/signup/client
    pour ne jamais avoir quatre formules différentes."""
    if not prospect_ids:
        return {}
    rows = (
        ProspectSignal.objects.filter(prospect_id__in=prospect_ids)
        .values("signal_type").annotate(n=Count("prospect_id", distinct=True))
        .order_by("-n")
    )
    return {row["signal_type"]: row["n"] for row in rows}


def signal_to_reply_counts():
    """signal -> réponse : prospects ayant répondu (ContactLog), par
    signal_type qu'ils portent."""
    replied_ids = _prospect_ids(ContactLog.objects.filter(outcome__in=["replied", "meeting", "proposal"]))
    return _signal_type_counts_for(replied_ids)


def signal_to_click_counts():
    """signal -> clic (EngagementEvent link_clicked)."""
    clicked_ids = _prospect_ids(EngagementEvent.objects.filter(event_type="link_clicked"))
    return _signal_type_counts_for(clicked_ids)


def signal_to_signup_counts():
    """signal -> signup (ConversionEvent event_type=signup)."""
    signup_ids = _prospect_ids(ConversionEvent.objects.filter(event_type="signup"))
    return _signal_type_counts_for(signup_ids)


def signal_to_client_counts():
    """signal -> client payant (ConversionEvent event_type=paying)."""
    client_ids = _prospect_ids(ConversionEvent.objects.filter(event_type="paying"))
    return _signal_type_counts_for(client_ids)


def conversion_rate_by_channel():
    """canal -> conversion : parmi les prospects contactés via chaque canal
    (ContactLog.channel), quelle proportion a converti (ConversionEvent, tout
    type confondu)."""
    rows = []
    for channel, label in ContactLog.CHANNELS:
        contacted_ids = _prospect_ids(ContactLog.objects.filter(channel=channel))
        if not contacted_ids:
            continue
        converted = ConversionEvent.objects.filter(prospect_id__in=contacted_ids).values("prospect_id").distinct().count()
        rows.append({
            "channel": channel, "label": label, "contacted": len(contacted_ids),
            "converted": converted,
            "rate": round(100 * converted / len(contacted_ids), 1),
        })
    return rows


def conversion_rate_by_intent_band():
    """Intent band -> conversion. Réutilise IN_MARKET_LEVELS (services/
    in_market_status.py) pour les bandes — jamais un second découpage."""
    rows = []
    for low, high, code, label in IN_MARKET_LEVELS:
        band_ids = set(Prospect.objects.filter(intent_score__gte=low, intent_score__lte=high).values_list("id", flat=True))
        converted = ConversionEvent.objects.filter(prospect_id__in=band_ids).values("prospect_id").distinct().count() if band_ids else 0
        rows.append({
            "band": code, "label": label, "prospects": len(band_ids), "converted": converted,
            "rate": round(100 * converted / len(band_ids), 1) if band_ids else 0,
        })
    return rows


def mrr_by_signal_type():
    """MRR total attribué, réparti par signal_type porté par le prospect
    converti. Un prospect avec plusieurs signal_type distincts contribue son
    MRR à chacun (multi-attribution volontaire) mais jamais deux fois au
    MÊME signal_type (join fait côté Python, pas un annotate() qui
    multiplierait les lignes par un produit croisé ProspectSignal x
    RevenueAttribution)."""
    mrr_by_prospect = dict(
        RevenueAttribution.objects.values("prospect_id").annotate(total=Sum("mrr")).values_list("prospect_id", "total")
    )
    if not mrr_by_prospect:
        return {}
    result = {}
    pairs = (
        ProspectSignal.objects.filter(prospect_id__in=mrr_by_prospect.keys())
        .values_list("prospect_id", "signal_type").distinct()
    )
    for prospect_id, signal_type in pairs:
        result[signal_type] = result.get(signal_type, 0) + mrr_by_prospect[prospect_id]
    return result


def mrr_by_channel():
    """MRR total par canal — réutilise RevenueAttribution.email_step et
    EmailStep.channel (bloc C), aucun nouveau champ de canal."""
    rows = (
        RevenueAttribution.objects.values("email_step__channel")
        .annotate(total_mrr=Sum("mrr"), conversions=Count("id"))
        .order_by("-total_mrr")
    )
    return [
        {
            "channel": row["email_step__channel"] or "inconnu",
            "mrr": row["total_mrr"] or 0,
            "conversions": row["conversions"],
        }
        for row in rows
    ]
