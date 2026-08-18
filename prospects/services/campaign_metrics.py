"""ETAPE 18/29 (mission 4) — métriques agrégées par campagne, sans N+1.

Une requête groupée (values().annotate()) par table liée plutôt qu'un
annotate() combiné sur plusieurs relations, qui dupliquerait les lignes par
jointures croisées et fausserait les comptes.
"""
from django.db.models import Count, Sum

from ..models import CampaignProspect, ConversionEvent, EmailSend, EngagementEvent, RevenueAttribution

EMPTY_METRICS = {"sent": 0, "clicks": 0, "signups": 0, "clients": 0, "mrr": 0}


def _grouped_counts(queryset, field):
    return {row[field]: row["n"] for row in queryset.values(field).annotate(n=Count("id"))}


def campaign_metrics_by_id(campaign_ids):
    """Retourne {campaign_id: {sent, clicks, signups, clients, mrr}}."""
    campaign_ids = list(campaign_ids)
    if not campaign_ids:
        return {}

    sent = _grouped_counts(
        EmailSend.objects.filter(campaign_prospect__campaign_id__in=campaign_ids, status="sent", is_test=False),
        "campaign_prospect__campaign_id",
    )
    clicks = _grouped_counts(
        EngagementEvent.objects.filter(campaign_id__in=campaign_ids, event_type="link_clicked"),
        "campaign_id",
    )
    signups = _grouped_counts(
        ConversionEvent.objects.filter(campaign_id__in=campaign_ids, event_type="signup"),
        "campaign_id",
    )
    clients = _grouped_counts(
        CampaignProspect.objects.filter(campaign_id__in=campaign_ids, status="paying"),
        "campaign_id",
    )
    mrr_rows = (
        RevenueAttribution.objects.filter(campaign_id__in=campaign_ids)
        .values("campaign_id").annotate(total=Sum("mrr"))
    )
    mrr = {row["campaign_id"]: row["total"] or 0 for row in mrr_rows}

    return {
        cid: {
            "sent": sent.get(cid, 0),
            "clicks": clicks.get(cid, 0),
            "signups": signups.get(cid, 0),
            "clients": clients.get(cid, 0),
            "mrr": mrr.get(cid, 0),
        }
        for cid in campaign_ids
    }


def annotate_campaigns_with_metrics(campaigns):
    """Attache `.metrics` à chaque Campaign d'une liste/queryset déjà évaluée."""
    campaigns = list(campaigns)
    metrics = campaign_metrics_by_id([c.pk for c in campaigns])
    for campaign in campaigns:
        campaign.metrics = metrics.get(campaign.pk, dict(EMPTY_METRICS))
    return campaigns


def campaign_performance_summary(campaign):
    return campaign_metrics_by_id([campaign.pk]).get(campaign.pk, dict(EMPTY_METRICS))
