"""Quality gate avant validation de la vague 24-28 août — deux correctifs
ciblés, aucun grand refactor :

D) une phrase de repli INTERNE (« Aucun signal spécifique confirmé... »,
   « Signaux insuffisants... ») ne doit jamais apparaître dans un e-mail
   commercial — le bloc concerné doit être omis, jamais remplacé par une
   invention ;
E) le sujet d'un e-mail est du texte brut — une apostrophe/esperluette dans
   un nom d'entreprise ne doit jamais devenir une entité HTML visible
   (`&#x27;`, `&amp;`...) dans le sujet SMTP réellement envoyé.
"""
from django.test import TestCase

from prospects.models import AgentBrief, CampaignProspect
from prospects.services.agent_brief import GENERIC_FALLBACK_NEED_PREFIX, INSUFFICIENT_SIGNAL_NEED_TEXT, _detected_need
from prospects.services.campaign_sending import get_or_create_default_sequence
from prospects.services.predictneed_email import render_predictneed_email, render_predictneed_subject

from .factories import make_campaign, make_compliance_profile, make_icp, make_product, make_prospect, make_public_email


class InternalFallbackNeedNeverLeaksToCustomerTests(TestCase):
    """Section D."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, self.icp)
        self.sequence = get_or_create_default_sequence(self.product, self.icp)
        self.step = self.sequence.steps.order_by("order").first()
        self.variant = self.step.variants.first()

    def _member_with_brief(self, detected_need, relevant_signals=None):
        prospect = make_prospect(name="Prospect Test", siret="00000000012345")
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect, status="selected")
        brief = AgentBrief.objects.create(
            prospect=prospect, product=self.product, icp=self.icp,
            detected_need=detected_need, relevant_signals=relevant_signals or [],
        )
        member.agent_brief = brief
        member.save(update_fields=["agent_brief"])
        return member

    def test_generic_fallback_need_is_never_shown_to_prospect(self):
        # Reproduit exactement le texte réel généré par _detected_need() quand
        # aucun signal intent n'existe mais que le produit a un target_problem.
        detected_need = f"{GENERIC_FALLBACK_NEED_PREFIX} Problème générique adressé par {self.product.name} : {self.product.target_problem}"
        member = self._member_with_brief(detected_need, relevant_signals=[{"label": "Google Tag Manager installé"}])

        _subject, html, text = render_predictneed_email(member, self.step, self.variant)

        self.assertNotIn(GENERIC_FALLBACK_NEED_PREFIX, html)
        self.assertNotIn(GENERIC_FALLBACK_NEED_PREFIX, text)
        self.assertNotIn("Problème générique adressé par", html)
        self.assertNotIn("Problème générique adressé par", text)
        # Le VRAI signal (relevant_signals), lui, doit rester présent.
        self.assertIn("Google Tag Manager installé", text)

    def test_insufficient_signal_need_is_never_shown_to_prospect(self):
        member = self._member_with_brief(INSUFFICIENT_SIGNAL_NEED_TEXT, relevant_signals=[])

        _subject, html, text = render_predictneed_email(member, self.step, self.variant)

        self.assertNotIn(INSUFFICIENT_SIGNAL_NEED_TEXT, html)
        self.assertNotIn(INSUFFICIENT_SIGNAL_NEED_TEXT, text)
        self.assertNotIn("Signaux insuffisants", html)
        self.assertNotIn("Signaux insuffisants", text)

    def test_real_observed_need_is_still_shown(self):
        """Contrôle : un besoin RÉELLEMENT détecté (signaux intent réels,
        jamais un des deux textes de repli) doit continuer à s'afficher
        normalement — ce correctif ne doit rien omettre de légitime."""
        real_need = "Signaux observés chez ce prospect pouvant indiquer un besoin : Formulaire de demande de devis détecté."
        member = self._member_with_brief(real_need, relevant_signals=[{"label": "Formulaire de contact présent"}])

        _subject, html, text = render_predictneed_email(member, self.step, self.variant)

        self.assertIn("Formulaire de demande de devis détecté", html)
        self.assertIn("Formulaire de demande de devis détecté", text)

    def test_agent_brief_generation_itself_is_unchanged(self):
        """_detected_need() (génération du brief, destinée à un opérateur)
        n'est pas modifiée par ce correctif — seul le RENDU commercial omet
        ces deux textes précis, jamais leur génération/stockage."""
        prospect = make_prospect(name="Sans Signal", siret="00000000054321")
        need = _detected_need(prospect, self.product)
        self.assertTrue(
            need.startswith(GENERIC_FALLBACK_NEED_PREFIX) or need == INSUFFICIENT_SIGNAL_NEED_TEXT,
        )


class SubjectPlainTextRenderingTests(TestCase):
    """Section E."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, self.icp)
        self.sequence = get_or_create_default_sequence(self.product, self.icp)
        self.step = self.sequence.steps.order_by("order").first()
        self.variant = self.step.variants.first()

    def _render_subject_for(self, company_name):
        prospect = make_prospect(name=company_name, siret="00000000099001")
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect, status="selected")
        subject, _html, _text = render_predictneed_email(member, self.step, self.variant)
        return subject

    def test_apostrophe_in_company_name_stays_plain_text(self):
        subject = self._render_subject_for("ACTION'ELLES")
        self.assertIn("ACTION'ELLES", subject)
        self.assertNotIn("&#x27;", subject)
        self.assertNotIn("&#39;", subject)

    def test_ampersand_in_company_name_stays_plain_text(self):
        subject = self._render_subject_for("SMITH & CO")
        self.assertIn("SMITH & CO", subject)
        self.assertNotIn("&amp;", subject)

    def test_accented_characters_in_company_name_are_preserved(self):
        subject = self._render_subject_for("ÉTABLISSEMENTS DÉCÉLÉRÉ")
        self.assertIn("ÉTABLISSEMENTS DÉCÉLÉRÉ", subject)

    def test_html_body_still_escapes_the_same_special_characters(self):
        """Contrôle : la protection d'échappement du CORPS HTML (via
        escape(), jamais Template autoescape) reste intacte — ce correctif
        ne touche QUE le rendu du sujet. Nécessite un AgentBrief avec une
        observation pour que company_name apparaisse dans le corps (sans
        signal, le bloc d'observation est simplement absent)."""
        prospect = make_prospect(name="ACTION'ELLES", siret="00000000099002")
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect, status="selected")
        brief = AgentBrief.objects.create(
            prospect=prospect, product=self.product, icp=self.icp,
            detected_need="Signaux observés chez ce prospect : formulaire de contact détecté.",
            relevant_signals=[{"label": "formulaire de contact présent"}],
        )
        member.agent_brief = brief
        member.save(update_fields=["agent_brief"])
        _subject, html, _text = render_predictneed_email(member, self.step, self.variant)
        self.assertIn("ACTION&#x27;ELLES", html)

    def test_render_predictneed_subject_directly_never_escapes(self):
        ctx = {"company_name": "L'AGENCE & FILS", "detected_signal": "", "observation": ""}
        subject = render_predictneed_subject(None, ctx)
        self.assertIn("L'AGENCE & FILS", subject)
        self.assertNotIn("&#x27;", subject)
        self.assertNotIn("&amp;", subject)
