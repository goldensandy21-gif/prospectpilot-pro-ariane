# Correctif d'audit (round 2, puis round 3) — migration explicite et
# déterministe des poids ICP existants vers la formule qui inclut
# intent/engagement.
#
# Ce que cette migration NE fait PAS : elle ne fusionne pas silencieusement
# les anciens poids (qui totalisent déjà 100%) avec les nouveaux poids
# intent/engagement puis renormalise à la volée à chaque lecture — ça
# diluerait chaque ancien poids sans que personne ne l'ait décidé
# explicitement, et cette dilution serait recalculée différemment à chaque
# appel de effective_weights() selon ce qui se trouve dans le champ.
#
# Trois cas explicitement distingués (round 3) :
#   Cas A — `weights` égal EXACTEMENT aux anciens défauts (30/25/20/15/10) :
#     ce profil n'a jamais été réellement personnalisé, il porte juste le
#     défaut que l'ancien seed_data.py écrivait. Migré vers les NOUVEAUX
#     défauts exacts (25/15/15/15/5/15/10) — pas une règle générique de
#     rescale à 75%, qui donnerait des chiffres légèrement différents
#     (22/19/15/11/8) sans raison, alors que le produit a une intention
#     précise pour ces nouveaux défauts.
#   Cas B — profil réellement personnalisé (différent des anciens
#     défauts) : ses 5 poids historiques sont ramenés à EXACTEMENT 75% du
#     total en conservant leurs proportions RELATIVES (méthode du plus
#     grand reste — jamais un round() naïf qui peut dériver de la somme
#     cible), intent=15/engagement=10 complètent les 25% restants. Le
#     total écrit vaut TOUJOURS exactement 100, jamais 99 ou 101.
#   Cas C — `weights` vide (jamais personnalisé) : laissé vide,
#     `DEFAULT_ICP_WEIGHTS` (models/acquisition.py) s'applique déjà en
#     entier, sans dilution possible puisqu'il n'y a rien à fusionner.
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
NEW_DEFAULT_WEIGHTS = {
    "icp_fit": 25,
    "need": 15,
    "acquisition_maturity": 15,
    "contactability": 15,
    "timing": 5,
    "intent": 15,
    "engagement": 10,
}
LEGACY_ENVELOPE_PERCENT = 75
NEW_INTENT_WEIGHT = 15
NEW_ENGAGEMENT_WEIGHT = 10


def _round_preserving_total(raw_values, target_total):
    """Arrondit un dict de floats en entiers dont la somme vaut EXACTEMENT
    target_total, via la méthode du plus grand reste (Hamilton) — jamais un
    round() naïf qui peut dériver de +-1 ou +-2 par clé."""
    floors = {key: int(value) for key, value in raw_values.items()}
    remainder_needed = target_total - sum(floors.values())
    ordered_by_remainder = sorted(
        raw_values.keys(), key=lambda key: raw_values[key] - floors[key], reverse=True,
    )
    result = dict(floors)
    for key in ordered_by_remainder[:remainder_needed]:
        result[key] += 1
    return result


def backfill_icp_weights(apps, schema_editor):
    ICPProfile = apps.get_model("prospects", "ICPProfile")
    db_alias = schema_editor.connection.alias

    for icp in ICPProfile.objects.using(db_alias).all():
        weights = icp.weights or {}
        if not weights:
            continue  # Cas C : jamais personnalisé, laissé vide.
        if "intent" in weights or "engagement" in weights:
            continue  # déjà migré (ou créé après ce correctif).

        legacy = dict(weights)
        for key, default_value in OLD_LEGACY_DEFAULTS.items():
            legacy.setdefault(key, default_value)

        if all(legacy.get(key) == value for key, value in OLD_LEGACY_DEFAULTS.items()):
            # Cas A : exactement les anciens défauts -> nouveaux défauts exacts.
            new_weights = dict(NEW_DEFAULT_WEIGHTS)
        else:
            # Cas B : profil réellement personnalisé -> proportions relatives
            # conservées, ramenées à exactement 75% (somme exacte garantie).
            legacy_total = sum(legacy.get(key, 0) for key in OLD_LEGACY_DEFAULTS) or 1
            raw = {
                key: legacy.get(key, 0) * LEGACY_ENVELOPE_PERCENT / legacy_total
                for key in OLD_LEGACY_DEFAULTS
            }
            new_weights = _round_preserving_total(raw, LEGACY_ENVELOPE_PERCENT)
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
