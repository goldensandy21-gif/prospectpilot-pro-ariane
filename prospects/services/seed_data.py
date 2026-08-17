"""Données initiales idempotentes pour le moteur d'acquisition PredictNeed IA.

Toutes les fonctions utilisent update_or_create/get_or_create : elles peuvent être
rejouées sans effet de bord ni doublon (ETAPE 28 — initialize_app doit rester sûr).

Aucune information juridique n'est inventée ici : les champs de conformité restent
vides tant qu'ils ne sont pas fournis via variables d'environnement ou saisis en admin.
"""
import os

from ..models import (
    Competitor,
    EmailComplianceProfile,
    ICPProfile,
    ProductProfile,
    SearchPreset,
)


def seed_predictneed_product():
    product, _ = ProductProfile.objects.update_or_create(
        slug="predictneed-ia",
        defaults={
            "name": "PredictNeed IA",
            "active": True,
            "website_url": os.getenv("PREDICTNEED_WEBSITE_URL", ""),
            "simulator_url": os.getenv("PREDICTNEED_SIMULATOR_URL", ""),
            "signup_url": os.getenv("PREDICTNEED_SIGNUP_URL", ""),
            "pricing_url": os.getenv("PREDICTNEED_PRICING_URL", ""),
            "monthly_price": os.getenv("PREDICTNEED_MONTHLY_PRICE", "99.00"),
            "currency": "EUR",
            "short_value_proposition": (
                "PredictNeed IA analyse le comportement des visiteurs d'un site pour estimer "
                "leur besoin et leur intention, et recommander une action commerciale."
            ),
            "long_value_proposition": (
                "PredictNeed IA observe les pages visitées, le temps passé et les interactions "
                "des visiteurs d'un site professionnel pour calculer un score d'intention, "
                "identifier un profil et un besoin probable, puis suggérer la prochaine action "
                "commerciale la plus pertinente."
            ),
            "target_problem": (
                "Les visiteurs qui montrent un intérêt réel sans remplir de formulaire restent "
                "difficiles à repérer et à prioriser pour les équipes commerciales."
            ),
            "primary_cta_label": "Tester le simulateur PredictNeed IA",
            "primary_cta_url": os.getenv("PREDICTNEED_SIMULATOR_URL", ""),
            "features": [
                "Score d'intention par session visiteur",
                "Détection du profil et du besoin probable",
                "Recommandation d'action commerciale",
                "Suivi multicanal (UTM, connecteurs publicitaires)",
            ],
            "differentiators": [],
            "competitor_notes": {},
            "sender_brand_name": "PredictNeed IA",
            "sender_name": os.getenv("EMAIL_SENDER_NAME_PREDICTNEED", "PredictNeed IA"),
            "sender_email": os.getenv("PREDICTNEED_SENDER_EMAIL", "contact-predict@predictneed-ia.com"),
            "reply_to_email": os.getenv("PREDICTNEED_REPLY_TO_EMAIL", "contact-predict@predictneed-ia.com"),
            "logo_url": os.getenv("PREDICTNEED_LOGO_URL", ""),
            "contact_url": os.getenv("PREDICTNEED_CONTACT_URL", ""),
        },
    )
    return product


def seed_predictneed_compliance_profile(product):
    profile, _ = EmailComplianceProfile.objects.update_or_create(
        product=product,
        defaults={
            "organization_name": "PredictNeed IA",
            "legal_name": os.getenv("COMPANY_LEGAL_NAME", ""),
            "postal_address": os.getenv("COMPANY_POSTAL_ADDRESS_PREDICTNEED", ""),
            "country": os.getenv("COMPANY_COUNTRY", ""),
            "company_registration_number": os.getenv("COMPANY_REGISTRATION_NUMBER", ""),
            "contact_email": os.getenv("CONTACT_EMAIL_PREDICTNEED", "contact-predict@predictneed-ia.com"),
            "privacy_contact_email": os.getenv("PRIVACY_CONTACT_EMAIL", ""),
            "dpo_email": os.getenv("DPO_EMAIL", ""),
            "privacy_policy_url": os.getenv("PRIVACY_POLICY_URL", ""),
            "legal_notice_url": os.getenv("LEGAL_NOTICE_URL", ""),
            "data_rights_url": os.getenv("DATA_RIGHTS_URL", ""),
            "default_legal_basis": os.getenv("DEFAULT_LEGAL_BASIS", ""),
            "default_purpose": (
                "Prospection commerciale B2B en lien avec l'activité professionnelle du destinataire."
            ),
            "active": True,
        },
    )
    return profile


ICP_DEFINITIONS = [
    {
        "name": "Agences marketing / web",
        "description": "Agences web, marketing digital, communication qui pilotent déjà du trafic payant ou organique.",
        "target_sectors": ["Agence web", "Agence marketing", "Agence de communication"],
        "naf_sections": ["M"],
        "naf_codes": ["7311Z", "7312Z", "6201Z", "7021Z"],
        "employee_min": 5,
        "employee_max": 50,
        "positive_signals": ["gtm_detected", "meta_pixel_detected", "analytics_detected", "landing_pages"],
        "negative_signals": [],
        "minimum_outbound_score": 50,
    },
    {
        "name": "Services B2B",
        "description": "Sociétés de services B2B avec un parcours de conversion (devis, prise de rendez-vous).",
        "target_sectors": ["Conseil", "Services aux entreprises"],
        "naf_sections": ["M", "N"],
        "naf_codes": [],
        "employee_min": 5,
        "employee_max": 250,
        "positive_signals": ["contact_form", "pricing_page", "crm_detected"],
        "negative_signals": [],
        "minimum_outbound_score": 50,
    },
    {
        "name": "Centres et organismes de formation",
        "description": "Organismes de formation, y compris certifiés Qualiopi, avec inscriptions en ligne.",
        "target_sectors": ["Formation professionnelle"],
        "naf_sections": ["P"],
        "naf_codes": ["8559A", "8559B"],
        "employee_min": 2,
        "employee_max": 100,
        "positive_signals": ["booking_detected", "signup_form", "pricing_page"],
        "negative_signals": [],
        "minimum_outbound_score": 45,
    },
    {
        "name": "Conseil / consultants structurés",
        "description": "Cabinets de conseil structurés (plusieurs consultants), avec offres de services identifiables.",
        "target_sectors": ["Conseil en gestion", "Conseil stratégie"],
        "naf_sections": ["M"],
        "naf_codes": ["7022Z"],
        "employee_min": 3,
        "employee_max": 100,
        "positive_signals": ["pricing_page", "multiple_services", "contact_form"],
        "negative_signals": [],
        "minimum_outbound_score": 50,
    },
]


def seed_icp_profiles(product):
    created = []
    for definition in ICP_DEFINITIONS:
        icp, _ = ICPProfile.objects.update_or_create(
            product=product,
            name=definition["name"],
            defaults={
                "active": True,
                "description": definition["description"],
                "target_sectors": definition["target_sectors"],
                "naf_codes": definition["naf_codes"],
                "naf_sections": definition["naf_sections"],
                "company_categories": [],
                "employee_bands": [],
                "employee_min": definition["employee_min"],
                "employee_max": definition["employee_max"],
                "revenue_min": None,
                "revenue_max": None,
                "regions": [],
                "departments": [],
                "cities": [],
                "min_company_age_years": None,
                "max_company_age_years": None,
                "required_signals": [],
                "positive_signals": definition["positive_signals"],
                "negative_signals": definition["negative_signals"],
                "excluded_signals": [],
                "excluded_domains": [],
                "excluded_sectors": [],
                "weights": {
                    "icp_fit": 30,
                    "need": 25,
                    "acquisition_maturity": 20,
                    "contactability": 15,
                    "timing": 10,
                },
                "minimum_outbound_score": definition["minimum_outbound_score"],
            },
        )
        created.append(icp)
    return created


COMPETITOR_DEFINITIONS = [
    {
        "name": "Hotjar",
        "category": "behaviour_analytics",
        "positioning": "Heatmaps et enregistrements de session pour comprendre le comportement des visiteurs.",
        "relation_to_predictneed": "Complémentaire : observe le comportement mais ne score pas l'intention ni ne recommande d'action commerciale.",
        "suggested_angle": "Le prospect a déjà une culture de la donnée comportementale : PredictNeed va plus loin en priorisant l'action commerciale.",
        "scoring_penalty": 0,
        "scoring_bonus": 8,
    },
    {
        "name": "Microsoft Clarity",
        "category": "behaviour_analytics",
        "positioning": "Heatmaps et enregistrements de session gratuits.",
        "relation_to_predictneed": "Complémentaire : indique une maturité comportementale sans scoring commercial.",
        "suggested_angle": "Le prospect suit déjà le comportement visiteur gratuitement : PredictNeed ajoute la priorisation commerciale.",
        "scoring_penalty": 0,
        "scoring_bonus": 6,
    },
    {
        "name": "Contentsquare",
        "category": "behaviour_analytics",
        "positioning": "Plateforme d'expérience digitale pour grandes organisations.",
        "relation_to_predictneed": "Indique une maturité digitale élevée ; dans une grande organisation, peut signaler un budget déjà alloué ailleurs.",
        "suggested_angle": "Selon la taille de l'entreprise, positionner PredictNeed comme complément agile plutôt que remplacement.",
        "scoring_penalty": 4,
        "scoring_bonus": 4,
    },
    {
        "name": "Dealfront / Leadfeeder",
        "category": "visitor_intelligence",
        "positioning": "Identification des entreprises visitant un site B2B (reverse IP lookup).",
        "relation_to_predictneed": "Concurrent partiel : identifie les entreprises visiteuses mais ne score pas l'intention individuelle du visiteur.",
        "suggested_angle": "PredictNeed complète l'identification d'entreprise par un score d'intention et une recommandation d'action.",
        "scoring_penalty": 6,
        "scoring_bonus": 0,
    },
    {
        "name": "Leadinfo",
        "category": "visitor_intelligence",
        "positioning": "Identification des entreprises visiteuses et alertes commerciales.",
        "relation_to_predictneed": "Concurrent partiel sur l'identification d'entreprise, sans scoring d'intention comportementale détaillé.",
        "suggested_angle": "Mettre en avant le scoring d'intention et la recommandation d'action, absents des outils d'identification pure.",
        "scoring_penalty": 6,
        "scoring_bonus": 0,
    },
]


def seed_competitors():
    created = []
    for definition in COMPETITOR_DEFINITIONS:
        competitor, _ = Competitor.objects.update_or_create(
            name=definition["name"],
            defaults={
                "category": definition["category"],
                "positioning": definition["positioning"],
                "relation_to_predictneed": definition["relation_to_predictneed"],
                "suggested_angle": definition["suggested_angle"],
                "scoring_penalty": definition["scoring_penalty"],
                "scoring_bonus": definition["scoring_bonus"],
                "active": True,
            },
        )
        created.append(competitor)
    return created


def seed_search_presets(product, icps_by_name):
    presets = [
        {
            "name": "Agences marketing France 5-50 salariés",
            "icp_name": "Agences marketing / web",
            "filters": {"region": "France"},
            "volume_max_candidates": 1000,
            "volume_max_enrich": 200,
            "score_threshold": 65,
        },
        {
            "name": "Organismes de formation Qualiopi",
            "icp_name": "Centres et organismes de formation",
            "filters": {"est_qualiopi": True},
            "volume_max_candidates": 800,
            "volume_max_enrich": 150,
            "score_threshold": 55,
        },
        {
            "name": "Services B2B à forte maturité digitale",
            "icp_name": "Services B2B",
            "filters": {},
            "volume_max_candidates": 1000,
            "volume_max_enrich": 200,
            "score_threshold": 60,
        },
    ]
    created = []
    for definition in presets:
        icp = icps_by_name.get(definition["icp_name"])
        if not icp:
            continue
        preset, _ = SearchPreset.objects.update_or_create(
            name=definition["name"],
            defaults={
                "product": product,
                "icp": icp,
                "filters": definition["filters"],
                "volume_max_candidates": definition["volume_max_candidates"],
                "volume_max_enrich": definition["volume_max_enrich"],
                "score_threshold": definition["score_threshold"],
                "active": True,
            },
        )
        created.append(preset)
    return created


def run_all_seeds():
    product = seed_predictneed_product()
    seed_predictneed_compliance_profile(product)
    icps = seed_icp_profiles(product)
    seed_competitors()
    icps_by_name = {icp.name: icp for icp in icps}
    presets = seed_search_presets(product, icps_by_name)
    return {
        "product": product,
        "icps": icps,
        "presets": presets,
    }
