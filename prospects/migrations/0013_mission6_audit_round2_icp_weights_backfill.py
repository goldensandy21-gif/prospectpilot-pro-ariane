# Correctif d'audit (round 2) — migration explicite et déterministe des
# poids ICP existants vers la formule qui inclut intent/engagement.
#
# Ce que cette migration NE fait PAS : elle ne fusionne pas silencieusement
# les anciens poids (qui totalisent déjà 100%) avec les nouveaux poids
# intent/engagement puis renormalise à la volée à chaque lecture — ça
# diluerait chaque ancien poids d'environ 20% sans que personne ne l'ait
# décidé explicitement, et cette dilution serait recalculée différemment à
# chaque appel de effective_weights() selon ce qui se trouve dans le champ.
#
# Ce qu'elle fait : pour chaque ICPProfile dont `weights` est déjà renseigné
# (non vide) mais ne contient pas encore "intent"/"engagement", elle réécrit
# `weights` UNE FOIS, de façon déterministe :
#   - les 5 poids historiques (icp_fit/need/acquisition_maturity/
#     contactability/timing) sont conservés dans leurs proportions RELATIVES
#     exactes (ex. si icp_fit représentait 30% du total legacy, il continue
#     de représenter 30% des 75% qui restent) ;
#   - ils sont ramenés collectivement à 75% du nouveau total ;
#   - intent=15 et engagement=10 complètent explicitement les 25% restants.
# Résultat toujours écrit en clair dans la ligne — jamais une réinterprétation
# implicite calculée à la lecture.
#
# Les ICPProfile dont `weights` est VIDE (jamais personnalisé) ne sont PAS
# touchés : effective_weights() leur applique déjà entièrement
# DEFAULT_ICP_WEIGHTS (models/acquisition.py), sans dilution possible
# puisqu'il n'y a rien à fusionner.
#
# Migration purement RunPython sur une table JSONField, sans schéma modifié,
# sans nouvel index — aucun risque de l'incident "pending trigger events".
from django.db import migrations

OLD_LEGACY_DEFAULTS = {
    "icp_fit": 30,
    "need": 25,
    "acquisition_maturity": 20,
    "contactability": 15,
    "timing": 10,
}
LEGACY_ENVELOPE_PERCENT = 75
NEW_INTENT_WEIGHT = 15
NEW_ENGAGEMENT_WEIGHT = 10


def backfill_icp_weights(apps, schema_editor):
    ICPProfile = apps.get_model("prospects", "ICPProfile")
    db_alias = schema_editor.connection.alias

    for icp in ICPProfile.objects.using(db_alias).all():
        weights = icp.weights or {}
        if not weights:
            continue  # jamais personnalisé : DEFAULT_ICP_WEIGHTS s'applique déjà en entier
        if "intent" in weights or "engagement" in weights:
            continue  # déjà migré (ou créé après ce correctif)

        legacy = dict(weights)
        for key, default_value in OLD_LEGACY_DEFAULTS.items():
            legacy.setdefault(key, default_value)
        legacy_total = sum(legacy.get(key, 0) for key in OLD_LEGACY_DEFAULTS) or 1

        new_weights = {
            key: round(legacy.get(key, 0) * LEGACY_ENVELOPE_PERCENT / legacy_total)
            for key in OLD_LEGACY_DEFAULTS
        }
        new_weights["intent"] = NEW_INTENT_WEIGHT
        new_weights["engagement"] = NEW_ENGAGEMENT_WEIGHT

        icp.weights = new_weights
        icp.save(update_fields=["weights"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("prospects", "0012_mission6_audit_round2_contactlog_outcomes"),
    ]

    operations = [
        migrations.RunPython(backfill_icp_weights, noop_reverse),
    ]
