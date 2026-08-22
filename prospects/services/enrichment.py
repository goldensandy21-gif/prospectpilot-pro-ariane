from dataclasses import dataclass, field
import re
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from ..models import (
    ContactPerson,
    EnrichmentRun,
    EnrichmentSource,
    Prospect,
    ProspectEvidence,
    PublicContactForm,
    PublicEmail,
    PublicPhone,
    PublicSocialLink,
)
from .commoncrawl import open_web_presence
from .crawler import crawl_site
from .site_discovery import discover_official_site


EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)
PHONE_RE = re.compile(r"(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}")
GENERIC_PREFIXES = {
    "contact", "info", "bonjour", "commercial", "sales", "hello",
    "support", "admin", "rh", "recrutement", "secretariat", "accueil",
}
FREE_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "live.com", "orange.fr", "wanadoo.fr", "free.fr", "laposte.net",
}


SOURCE_DEFINITIONS = {
    "public_registry": {
        "name": "API Recherche d'entreprises",
        "source_type": "public_registry",
        "requires_api_key": False,
        "base_url": "https://recherche-entreprises.api.gouv.fr",
        "legal_notes": "Registre public d'entreprises. Ne fournit pas d'emails personnels.",
    },
    "company_website": {
        "name": "Site officiel de l'entreprise",
        "source_type": "company_website",
        "requires_api_key": False,
        "legal_notes": "Pages publiques du site officiel, avec respect de robots.txt.",
    },
    "common_crawl": {
        "name": "Common Crawl",
        "source_type": "open_web",
        "requires_api_key": False,
        "base_url": "https://commoncrawl.org",
        "legal_notes": "Index ouvert du web, utilisé pour indices de présence publique.",
    },
    "user_import": {
        "name": "Import CSV/Excel utilisateur",
        "source_type": "user_import",
        "requires_api_key": False,
        "legal_notes": "Données fournies par l'utilisateur. Leur base légale doit être vérifiée par l'utilisateur.",
    },
    "dropcontact": {
        "name": "Dropcontact",
        "source_type": "b2b_api",
        "requires_api_key": True,
        "api_key_env": "DROPCONTACT_API_KEY",
        "legal_notes": "Connecteur préparé. Utiliser uniquement l'API officielle et un contrat conforme RGPD.",
    },
    "apollo": {
        "name": "Apollo",
        "source_type": "b2b_api",
        "requires_api_key": True,
        "api_key_env": "APOLLO_API_KEY",
        "legal_notes": "Connecteur préparé. Utiliser uniquement l'API officielle et les usages autorisés.",
    },
    "kaspr": {
        "name": "Kaspr",
        "source_type": "b2b_api",
        "requires_api_key": True,
        "api_key_env": "KASPR_API_KEY",
        "legal_notes": "Connecteur préparé. Ne pas contourner LinkedIn ni les protections d'accès.",
    },
    "lemlist": {
        "name": "Lemlist",
        "source_type": "b2b_api",
        "requires_api_key": True,
        "api_key_env": "LEMLIST_API_KEY",
        "legal_notes": "Connecteur préparé pour synchronisation/campagnes selon API officielle.",
    },
    "france_travail": {
        "name": "France Travail — Offres d'emploi",
        "source_type": "open_web",
        "requires_api_key": True,
        "api_key_env": "FRANCE_TRAVAIL_CLIENT_ID",
        "base_url": "https://api.francetravail.io",
        "legal_notes": (
            "API publique et gratuite (OAuth2 client-credentials, "
            "FRANCE_TRAVAIL_CLIENT_ID/FRANCE_TRAVAIL_CLIENT_SECRET). "
            "Dormant tant que ces identifiants ne sont pas configurés — "
            "voir docs/WEB_DATA_INTELLIGENCE.md."
        ),
    },
}


@dataclass
class EvidenceCandidate:
    field_name: str
    value: str
    value_type: str = "other"
    confidence_score: int = 50
    verification_status: str = "unverified"
    source_key: str = "unknown"
    source_url: str = ""
    notes: str = ""
    raw_payload: dict = field(default_factory=dict)


def normalize_value(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()[:500]


def normalize_phone(phone):
    return re.sub(r"[^0-9+]", "", phone or "")


def classify_email_type(email):
    local = (email or "").split("@", 1)[0].lower().strip()
    if local in GENERIC_PREFIXES:
        return "generic"
    if "." in local or "-" in local or "_" in local:
        return "personal"
    return "unknown"


def verify_email(email):
    email = (email or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return {"status": "invalid", "confidence": 0, "email_type": "unknown"}

    domain = email.split("@", 1)[1]
    email_type = classify_email_type(email)
    confidence = 70
    status = "format_valid"

    if email_type == "generic":
        confidence = 78
    elif email_type == "personal":
        confidence = 82

    if domain in FREE_EMAIL_DOMAINS:
        confidence = min(confidence, 45)
        status = "deliverability_unknown"

    return {"status": status, "confidence": confidence, "email_type": email_type}


def verify_phone(phone):
    normalized = normalize_phone(phone)
    if not normalized:
        return {"status": "invalid", "confidence": 0}
    if PHONE_RE.search(phone or "") or normalized.startswith("+33"):
        return {"status": "format_valid", "confidence": 76}
    if len(re.sub(r"\D", "", normalized)) >= 8:
        return {"status": "deliverability_unknown", "confidence": 55}
    return {"status": "invalid", "confidence": 20}


def source_for(key):
    definition = SOURCE_DEFINITIONS.get(key, {
        "name": key,
        "source_type": "other",
        "requires_api_key": False,
    })
    source, _ = EnrichmentSource.objects.update_or_create(
        key=key,
        defaults={
            "name": definition.get("name", key),
            "source_type": definition.get("source_type", "other"),
            "requires_api_key": definition.get("requires_api_key", False),
            "api_key_env": definition.get("api_key_env", ""),
            "base_url": definition.get("base_url", ""),
            "legal_notes": definition.get("legal_notes", ""),
            "enabled": True,
        },
    )
    return source


def split_person_from_email(email):
    local = (email or "").split("@", 1)[0].lower()
    if classify_email_type(email) != "personal":
        return "", "", ""

    bits = [x for x in re.split(r"[._-]+", local) if len(x) > 1]
    if len(bits) < 2:
        return "", "", ""

    first = bits[0].capitalize()
    last = bits[1].capitalize()
    return first, last, f"{first} {last}"


class PublicRegistrySource:
    key = "public_registry"

    def collect(self, prospect):
        source_url = prospect.source_url
        data = {
            "name": prospect.name,
            "legal_name": prospect.legal_name,
            "sector": prospect.sector,
            "naf_code": prospect.naf_code,
            "address": prospect.address,
            "city": prospect.city,
            "country": prospect.country,
            "postal_code": prospect.postal_code,
            "siren": prospect.siren,
            "siret": prospect.siret,
            "employee_band": prospect.employee_band,
        }
        candidates = []
        for field_name, value in data.items():
            if value:
                candidates.append(EvidenceCandidate(
                    field_name=field_name,
                    value=str(value),
                    value_type="company" if field_name not in {"address", "city", "country", "postal_code"} else "address",
                    confidence_score=86,
                    verification_status="verified",
                    source_key=self.key,
                    source_url=source_url,
                    raw_payload={"source_payload": prospect.source_payload},
                ))
        return candidates


class CompanyWebsiteSource:
    key = "company_website"

    def __init__(self, force_refresh=False):
        # Audit correctif §8 — cooldown avant de relancer un crawl réseau
        # complet pour ce même prospect ; force_refresh=True le contourne
        # explicitement (jamais silencieusement).
        self.force_refresh = force_refresh

    def _recently_crawled(self, prospect):
        if self.force_refresh:
            return False
        cutoff = timezone.now() - timezone.timedelta(
            minutes=getattr(settings, "WEB_ENRICHMENT_COOLDOWN_MINUTES", 60)
        )
        return prospect.enrichment_runs.filter(status="done", finished_at__gte=cutoff).exists()

    def collect(self, prospect):
        candidates = []
        website = prospect.website
        if not website:
            discovered = discover_official_site(
                prospect.name,
                prospect.city,
                max_candidates=getattr(settings, "SEARCH_SITE_CANDIDATES", 6),
            )
            website = discovered.get("url", "")
            if website:
                candidates.append(EvidenceCandidate(
                    field_name="website",
                    value=website,
                    value_type="website",
                    confidence_score=discovered.get("confidence", 70),
                    verification_status="verified",
                    source_key=self.key,
                    source_url=website,
                    raw_payload=discovered,
                ))

        if not website:
            return candidates

        if self._recently_crawled(prospect):
            # Un enrichissement a déjà réussi récemment pour ce prospect —
            # réutilise les ProspectEvidence/PublicEmail/ContactPerson déjà
            # persistés au lieu de relancer un crawl réseau identique.
            return candidates

        data = crawl_site(
            website,
            max_pages=getattr(settings, "SEARCH_SCAN_CRAWL_PAGES", 3),
            check_broken_links=False,
        )

        for page in data.get("pages", []):
            page_url = page.get("url", website)
            for email in page.get("found_emails", []):
                check = verify_email(email)
                if check["status"] == "invalid":
                    continue
                candidates.append(EvidenceCandidate(
                    field_name="email",
                    value=email.strip().lower(),
                    value_type="email",
                    confidence_score=check["confidence"],
                    verification_status=check["status"],
                    source_key=self.key,
                    source_url=page_url,
                    raw_payload={"email_type": check["email_type"]},
                ))

            for phone in page.get("found_phones", []):
                check = verify_phone(phone)
                if check["status"] == "invalid":
                    continue
                candidates.append(EvidenceCandidate(
                    field_name="phone",
                    value=phone.strip(),
                    value_type="phone",
                    confidence_score=check["confidence"],
                    verification_status=check["status"],
                    source_key=self.key,
                    source_url=page_url,
                ))

            for form in page.get("found_contact_forms", []):
                form_url = form.get("page_url") or page_url
                candidates.append(EvidenceCandidate(
                    field_name="contact_form",
                    value=form_url,
                    value_type="profile",
                    confidence_score=58,
                    verification_status="format_valid",
                    source_key=self.key,
                    source_url=form_url,
                    raw_payload=form,
                ))

            for link in page.get("found_social_links", []):
                url = link.get("url", "")
                if not url:
                    continue
                candidates.append(EvidenceCandidate(
                    field_name=f"social_{link.get('platform', 'other')}",
                    value=url,
                    value_type="profile",
                    confidence_score=62,
                    verification_status="format_valid",
                    source_key=self.key,
                    source_url=page_url,
                    raw_payload=link,
                ))

            # Mission 7E — Temporal Signal Intelligence : faits datés réels
            # trouvés sur cette même page (structured data / meta de date),
            # jamais collected_at. Confiance plus haute pour une date
            # structurée JSON-LD que pour un repli sur balise meta.
            for event in page.get("found_temporal_events", []):
                is_structured = event.get("source_method") == "json_ld_dated_content"
                candidates.append(EvidenceCandidate(
                    field_name=event["field_name"],
                    # Inclut l'URL de la page dans la valeur (donc dans la clé
                    # de dédoublonnage prospect+field_name+normalized_value) :
                    # un repli meta sans titre ne doit jamais écraser un autre
                    # évènement daté trouvé sur une page différente.
                    value=event.get("headline") or f"{event['field_name']} — {page_url}",
                    value_type="other",
                    confidence_score=85 if is_structured else 55,
                    verification_status="verified" if is_structured else "format_valid",
                    source_key=self.key,
                    source_url=page_url,
                    raw_payload={"event_date": event["event_date"], "headline": event.get("headline", ""), "method": event.get("source_method", "")},
                ))

            # Mission 7C — People Discovery : personnes trouvées sur cette même
            # page (schema.org Person, ou heuristique texte sur une page
            # équipe reconnue), déjà extraites en un seul passage par
            # analyze_html() — aucun deuxième crawl du site.
            for person in page.get("found_people", []):
                full_name = str(person.get("full_name") or "").strip()
                if not full_name:
                    continue
                is_structured = person.get("method") == "json_ld_person"
                candidates.append(EvidenceCandidate(
                    field_name="person",
                    value=full_name,
                    value_type="person",
                    confidence_score=80 if is_structured else 55,
                    verification_status="public_source_confirmed" if is_structured else "format_valid",
                    source_key=self.key,
                    source_url=page_url,
                    raw_payload=person,
                ))

        return candidates


class CommonCrawlSource:
    key = "common_crawl"

    def collect(self, prospect):
        if not prospect.website:
            return []
        try:
            data = open_web_presence(prospect.website)
        except Exception:
            return []
        return [EvidenceCandidate(
            field_name="open_web_presence",
            value=str(data.get("capture_count", 0)),
            value_type="other",
            confidence_score=45,
            verification_status="deliverability_unknown",
            source_key=self.key,
            source_url=prospect.website,
            notes="Présence détectée dans un index web ouvert.",
            raw_payload=data,
        )]


class ExternalB2BProviderSource:
    def __init__(self, key):
        self.key = key
        self.definition = SOURCE_DEFINITIONS[key]

    def collect(self, prospect):
        api_key_env = self.definition.get("api_key_env", "")
        api_key = getattr(settings, api_key_env, "") if api_key_env else ""
        if not api_key:
            return []

        return [EvidenceCandidate(
            field_name="external_provider_status",
            value=f"{self.definition['name']} configuré",
            value_type="other",
            confidence_score=30,
            verification_status="unverified",
            source_key=self.key,
            notes="Adaptateur prêt : brancher ici l'appel à l'API officielle du fournisseur selon votre contrat.",
        )]


def _france_travail_source():
    from .france_travail import FranceTravailSource
    return FranceTravailSource()


SOURCE_CLASSES = {
    "public_registry": PublicRegistrySource,
    "company_website": CompanyWebsiteSource,
    "common_crawl": CommonCrawlSource,
    "dropcontact": lambda: ExternalB2BProviderSource("dropcontact"),
    "apollo": lambda: ExternalB2BProviderSource("apollo"),
    "kaspr": lambda: ExternalB2BProviderSource("kaspr"),
    "lemlist": lambda: ExternalB2BProviderSource("lemlist"),
    # Mission 7E, section 11 — dormant tant qu'aucun identifiant n'est
    # configuré (voir france_travail.py::is_configured()). Import tardif :
    # france_travail.py importe EvidenceCandidate depuis ce module.
    "france_travail": _france_travail_source,
}
DEFAULT_SOURCE_KEYS = ["public_registry", "company_website", "common_crawl"]


class EnrichmentEngine:
    def __init__(self, source_keys=None, force_refresh=False):
        self.source_keys = source_keys or DEFAULT_SOURCE_KEYS
        # Audit correctif §8 — jamais deux crawls réseau identiques pour le
        # même prospect à quelques minutes d'écart (bulk enrich successifs) ;
        # force_refresh=True le contourne explicitement.
        self.force_refresh = force_refresh

    def sources(self):
        for key in self.source_keys:
            factory = SOURCE_CLASSES.get(key)
            if not factory:
                continue
            source_for(key)
            if key == "company_website":
                yield CompanyWebsiteSource(force_refresh=self.force_refresh)
            else:
                yield factory()

    def enrich_prospect(self, prospect, user=None):
        run = EnrichmentRun.objects.create(
            prospect=prospect,
            owner=user,
            status="running",
            mode="prospect",
            sources_requested=self.source_keys,
            started_at=timezone.now(),
        )
        totals = {"evidence": 0, "emails": 0, "phones": 0, "contacts": 0}
        completed = []

        try:
            for source_adapter in self.sources():
                source = source_for(source_adapter.key)
                for candidate in source_adapter.collect(prospect):
                    evidence = self.store_evidence(prospect, source, candidate)
                    if evidence:
                        totals["evidence"] += 1
                completed.append(source_adapter.key)

            self.apply_best_values(prospect)
            # Mission 7D, niveau B — propose un e-mail probable (jamais
            # "vérifié") pour les personnes découvertes sans e-mail connu,
            # seulement si un motif de domaine fiable existe déjà. Import
            # tardif : email_intelligence.py importe depuis ce module.
            from .email_intelligence import propose_inferred_emails_for_prospect
            totals["emails_inferred"] = len(propose_inferred_emails_for_prospect(prospect))
            totals["emails"] = prospect.public_emails.count()
            totals["phones"] = prospect.public_phones.count()
            totals["contacts"] = prospect.contact_people.count()

            # Audit correctif §2 — les ProspectEvidence fraîchement déposées
            # (temporelles, personnes, réseaux sociaux) doivent être
            # normalisées en ProspectSignal par les collecteurs concernés
            # AVANT de rendre la main, jamais laissées à un appel manuel
            # ultérieur. persist_signals() (signals.py) recalcule déjà
            # intent_score/engagement_score/predictneed_acquisition_score
            # dès qu'au moins un signal est sauvegardé — rien d'autre à faire
            # ici. Scopé aux collecteurs qui lisent exactement ce que cet
            # enrichissement vient de produire (temporel/décideurs/social) ;
            # technologie et quick-scan restent gérés par leur propre
            # pipeline (acquisition_pipeline.py) pour éviter une double
            # dérivation redondante.
            from .signal_collectors import (
                DecisionMakerSignalCollector, RecentActivitySignalCollector,
                SocialPresenceSignalCollector, run_signal_collectors,
            )
            saved_signals, signal_errors = run_signal_collectors(prospect, collectors=[
                RecentActivitySignalCollector(),
                DecisionMakerSignalCollector(),
                SocialPresenceSignalCollector(),
            ])
            totals["signals"] = len(saved_signals)
            run.status = "done"
            run.sources_completed = completed
            run.totals = totals
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "sources_completed", "totals", "finished_at"])
            return run
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            run.sources_completed = completed
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error", "sources_completed", "finished_at"])
            raise

    def store_evidence(self, prospect, source, candidate):
        value = str(candidate.value or "").strip()
        if not value:
            return None

        normalized = normalize_phone(value) if candidate.value_type == "phone" else normalize_value(value)
        existing = ProspectEvidence.objects.filter(
            prospect=prospect,
            field_name=candidate.field_name,
            normalized_value=normalized,
        ).first()
        now = timezone.now()

        if existing and existing.confidence_score > candidate.confidence_score:
            existing.last_checked_at = now
            existing.save(update_fields=["last_checked_at"])
            return existing

        evidence, _ = ProspectEvidence.objects.update_or_create(
            prospect=prospect,
            field_name=candidate.field_name,
            normalized_value=normalized,
            defaults={
                "source": source,
                "value": value,
                "value_type": candidate.value_type,
                "confidence_score": candidate.confidence_score,
                "verification_status": candidate.verification_status,
                "source_url": candidate.source_url,
                "notes": candidate.notes,
                "raw_payload": candidate.raw_payload,
                "is_current": True,
                "last_checked_at": now,
            },
        )

        if candidate.value_type == "email":
            self.store_email(prospect, source, candidate, evidence)
        elif candidate.value_type == "phone":
            self.store_phone(prospect, source, candidate, evidence)
        elif candidate.value_type == "person":
            self.store_person(prospect, source, candidate)
        elif candidate.field_name == "contact_form":
            PublicContactForm.objects.update_or_create(
                prospect=prospect,
                page_url=value,
                form_action=candidate.raw_payload.get("form_action", ""),
                defaults={
                    "form_method": candidate.raw_payload.get("form_method", ""),
                    "has_email_field": bool(candidate.raw_payload.get("has_email_field")),
                    "has_phone_field": bool(candidate.raw_payload.get("has_phone_field")),
                    "is_primary": not prospect.contact_forms.exists(),
                    "is_active": True,
                    "discovery_method": source.key,
                },
            )
        elif candidate.field_name.startswith("social_"):
            PublicSocialLink.objects.update_or_create(
                prospect=prospect,
                url=value,
                defaults={
                    "platform": candidate.raw_payload.get("platform", "other"),
                    "source_url": candidate.source_url,
                    "is_active": True,
                    "discovery_method": source.key,
                },
            )

        return evidence

    def store_email(self, prospect, source, candidate, evidence):
        # Audit correctif §4 — Email Finder niveau A : un e-mail passant par
        # ce chemin a été explicitement observé sur une page publique
        # (CompanyWebsiteSource est le seul appelant réel de store_email()) ;
        # classify_public_source_email() reclasse donc "format_valid" en
        # "public_source_confirmed" — jamais une deuxième logique de format,
        # elle appelle verify_email() en interne. Un domaine gratuit reste
        # "deliverability_unknown" (sémantique déjà correcte, inchangée).
        # Import tardif : email_intelligence.py importe depuis ce module.
        from .email_intelligence import classify_public_source_email
        email = normalize_value(candidate.value)
        check = classify_public_source_email(email)
        obj, created = PublicEmail.objects.update_or_create(
            prospect=prospect,
            email=email,
            defaults={
                "email_type": check["email_type"],
                "confidence_score": max(candidate.confidence_score, check["confidence"]),
                "verification_status": check["status"],
                "source_name": source.name,
                "source_url": candidate.source_url,
                "source_type": "contact_page" if "contact" in candidate.source_url.lower() else "website",
                "is_active": True,
                "discovery_method": source.key,
                "last_checked_at": timezone.now(),
            },
        )

        best = prospect.public_emails.filter(is_primary=True).first()
        if created or not best or obj.confidence_score >= best.confidence_score:
            PublicEmail.objects.filter(prospect=prospect).update(is_primary=False)
            obj.is_primary = True
            obj.save(update_fields=["is_primary"])
            prospect.public_email = obj.email
            prospect.save(update_fields=["public_email", "updated_at"])

        first, last, full = split_person_from_email(email)
        if full:
            ContactPerson.objects.update_or_create(
                prospect=prospect,
                email=email,
                defaults={
                    "source": source,
                    "first_name": first,
                    "last_name": last,
                    "full_name": full,
                    "source_url": candidate.source_url,
                    "confidence_score": min(75, obj.confidence_score),
                    "verification_status": obj.verification_status,
                    "last_checked_at": timezone.now(),
                    "raw_payload": {"inferred_from_email": True, "evidence_id": evidence.pk},
                },
            )

    def store_person(self, prospect, source, candidate):
        """Mission 7C — miroir de store_email()/store_phone() pour les
        personnes : dédup Entreprise + personne (nom, insensible à la casse),
        confiance la plus haute gagne, ne remplace jamais un poste/profil déjà
        connu par une valeur vide. N'écrit jamais si le nom est vide."""
        full_name = str(candidate.value or "").strip()
        if not full_name:
            return None
        payload = candidate.raw_payload or {}
        existing = ContactPerson.objects.filter(prospect=prospect, full_name__iexact=full_name).first()
        if existing and existing.confidence_score > candidate.confidence_score:
            existing.last_checked_at = timezone.now()
            existing.save(update_fields=["last_checked_at"])
            return existing

        obj = existing or ContactPerson(prospect=prospect, full_name=full_name)
        obj.source = source
        obj.job_title = str(payload.get("job_title") or "").strip() or obj.job_title
        obj.profile_url = str(payload.get("profile_url") or "").strip() or obj.profile_url
        obj.source_url = candidate.source_url
        obj.confidence_score = candidate.confidence_score
        obj.verification_status = candidate.verification_status
        obj.raw_payload = payload
        obj.is_active = True
        obj.last_checked_at = timezone.now()
        obj.save()
        return obj

    def store_phone(self, prospect, source, candidate, evidence):
        phone = str(candidate.value).strip()
        check = verify_phone(phone)
        obj, created = PublicPhone.objects.update_or_create(
            prospect=prospect,
            phone=phone,
            defaults={
                "confidence_score": max(candidate.confidence_score, check["confidence"]),
                "verification_status": check["status"],
                "source_name": source.name,
                "source_url": candidate.source_url,
                "source_type": "contact_page" if "contact" in candidate.source_url.lower() else "website",
                "is_active": True,
                "discovery_method": source.key,
                "last_checked_at": timezone.now(),
            },
        )

        best = prospect.public_phones.filter(is_primary=True).first()
        if created or not best or obj.confidence_score >= best.confidence_score:
            PublicPhone.objects.filter(prospect=prospect).update(is_primary=False)
            obj.is_primary = True
            obj.save(update_fields=["is_primary"])
            prospect.public_phone = obj.phone
            prospect.save(update_fields=["public_phone", "updated_at"])

    def apply_best_values(self, prospect):
        writable_fields = {
            "name", "legal_name", "sector", "naf_code", "address", "city",
            "country", "postal_code", "employee_band", "website",
        }
        changed = []
        for evidence in prospect.evidence_items.filter(is_current=True).order_by("-confidence_score"):
            if evidence.field_name not in writable_fields:
                continue
            current = getattr(prospect, evidence.field_name, "")
            if current and evidence.field_name != "website":
                continue
            if evidence.field_name == "website" and prospect.website_confidence > evidence.confidence_score:
                continue
            setattr(prospect, evidence.field_name, evidence.value)
            changed.append(evidence.field_name)
            if evidence.field_name == "website":
                prospect.website_confidence = evidence.confidence_score
                changed.append("website_confidence")

        if changed:
            changed.append("updated_at")
            prospect.save(update_fields=list(dict.fromkeys(changed)))


def create_external_source_records():
    for key in SOURCE_DEFINITIONS:
        source_for(key)
