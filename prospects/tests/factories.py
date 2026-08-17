from prospects.models import (
    Campaign,
    CampaignProspect,
    EmailComplianceProfile,
    ICPProfile,
    ProductProfile,
    Prospect,
    PublicEmail,
)


def make_product(**overrides):
    defaults = {
        "name": "PredictNeed IA", "slug": "predictneed-ia-test", "active": True,
        "website_url": "https://predictneed-ia.example", "simulator_url": "https://predictneed-ia.example/simulateur",
        "signup_url": "https://predictneed-ia.example/inscription",
        "sender_brand_name": "PredictNeed IA", "sender_name": "PredictNeed IA",
        "sender_email": "contact-predict@predictneed-ia.com", "reply_to_email": "contact-predict@predictneed-ia.com",
    }
    defaults.update(overrides)
    return ProductProfile.objects.create(**defaults)


def make_compliance_profile(product, ready=True, **overrides):
    defaults = {
        "organization_name": "PredictNeed IA", "contact_email": "contact-predict@predictneed-ia.com",
        "privacy_policy_url": "https://predictneed-ia.example/confidentialite" if ready else "",
        "active": True,
    }
    defaults.update(overrides)
    return EmailComplianceProfile.objects.create(product=product, **defaults)


def make_icp(product, **overrides):
    defaults = {
        "name": "Agences marketing / web", "active": True,
        "naf_codes": ["73.11Z"], "employee_min": 5, "employee_max": 50,
        "minimum_outbound_score": 50,
        "weights": {"icp_fit": 30, "need": 25, "acquisition_maturity": 20, "contactability": 15, "timing": 10},
    }
    defaults.update(overrides)
    return ICPProfile.objects.create(product=product, **defaults)


def make_prospect(**overrides):
    defaults = {
        "name": "Agence Exemple", "sector": "Agence web", "naf_code": "73.11Z",
        "city": "Lyon", "department": "69", "employee_band": "12",
        "website": "https://agence-exemple.example", "prospecting_allowed": True, "status": "new",
        "siret": "",
    }
    defaults.update(overrides)
    if not defaults.get("siret"):
        import uuid
        defaults["siret"] = uuid.uuid4().hex[:14]
    return Prospect.objects.create(**defaults)


def make_public_email(prospect, email="marie@agence-exemple.example", **overrides):
    defaults = {
        "email_type": "personal", "confidence_score": 80, "verification_status": "format_valid",
        "is_primary": True, "is_active": True,
    }
    defaults.update(overrides)
    obj = PublicEmail.objects.create(prospect=prospect, email=email, **defaults)
    prospect.public_email = email
    prospect.save(update_fields=["public_email"])
    return obj


def make_campaign(product, icp=None, **overrides):
    defaults = {
        "name": "Campagne test", "objective": "Test", "status": "draft",
        "score_threshold": 50, "daily_send_limit": 30, "total_limit": 200,
    }
    defaults.update(overrides)
    return Campaign.objects.create(product=product, icp=icp, **defaults)


def make_campaign_prospect(campaign, prospect, **overrides):
    defaults = {"acquisition_score_snapshot": 70, "grade": "B", "status": "selected"}
    defaults.update(overrides)
    return CampaignProspect.objects.create(campaign=campaign, prospect=prospect, **defaults)
