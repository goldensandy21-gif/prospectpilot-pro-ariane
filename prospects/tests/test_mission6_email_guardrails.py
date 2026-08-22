"""Mission 6, bloc 6 (correctif d'audit) — les garde-fous de personnalisation
s'appliquent aussi aux e-mails, pas seulement à LinkedIn. `product.target_problem`
ne doit jamais être présenté comme un besoin DÉTECTÉ chez un prospect précis
sans preuve correspondante."""
from django.test import TestCase
from django.utils import timezone

from prospects.models import CampaignProspect, ProspectSignal
from prospects.services.agent_brief import generate_agent_brief
from prospects.services.message_guardrails import assert_no_overclaiming
from prospects.services.predictneed_email import render_predictneed_email
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_campaign, make_icp, make_prospect, make_public_email, make_product


def _fit_signal(prospect, signal_type="analytics_detected", value="Google Analytics"):
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category="analytics", signal_group="fit",
        source_kind="technology", label="Outils d'analytics détectés", value=value,
        evidence="preuve", confidence=80, score_impact=6, positive=True, observed_at=timezone.now(),
        fingerprint=signal_fingerprint(signal_type, value, "preuve"),
    )


def _intent_signal(prospect, signal_type="hiring_growth", label="Recrutement Growth détecté"):
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category="timing", signal_group="intent",
        source_kind="open_web", label=label, evidence="preuve intent",
        confidence=75, score_impact=8, positive=True, observed_at=timezone.now(),
        fingerprint=signal_fingerprint(signal_type, "", "preuve intent"),
    )


class DetectedNeedNeverOverclaimsFromGenericProductProblemTests(TestCase):
    def setUp(self):
        self.product = make_product(target_problem="Les visiteurs qualifiés ne sont pas priorisés correctement.")
        self.icp = make_icp(self.product)

    def test_generic_product_problem_is_labeled_generic_not_detected(self):
        prospect = make_prospect()
        make_public_email(prospect)
        _fit_signal(prospect)  # seulement du FIT, aucun signal d'intent

        brief = generate_agent_brief(prospect, icp=self.icp, product=self.product)

        self.assertIn("Aucun signal spécifique confirmé", brief.detected_need)
        self.assertIn("générique", brief.detected_need.lower())
        self.assertEqual(assert_no_overclaiming(brief.detected_need), [])

    def test_real_intent_signal_is_cited_specifically_instead_of_generic_claim(self):
        prospect = make_prospect()
        make_public_email(prospect)
        _intent_signal(prospect)

        brief = generate_agent_brief(prospect, icp=self.icp, product=self.product)

        self.assertIn("Recrutement Growth détecté", brief.detected_need)
        self.assertIn("Signaux observés chez ce prospect", brief.detected_need)

    def test_no_signal_and_no_product_problem_falls_back_to_honest_default(self):
        product_without_problem = make_product(name="Autre Produit", slug="autre-produit", target_problem="")
        icp = make_icp(product_without_problem)
        prospect = make_prospect()
        brief = generate_agent_brief(prospect, icp=icp, product=product_without_problem)
        self.assertIn("Signaux insuffisants", brief.detected_need)


class RenderedEmailNeverOverclaimsTests(TestCase):
    """Le renderer e-mail doit passer par les mêmes affirmations factuelles
    autorisées — testé sur le texte RÉELLEMENT rendu, pas seulement sur le
    service qui le construit."""

    def setUp(self):
        # Copie produit réaliste (générique, jamais adressée comme une
        # affirmation sur UN prospect précis) — le risque d'overclaiming
        # testé ici vient du code (detected_need), pas d'une mauvaise copie.
        self.product = make_product(target_problem="Les visiteurs qualifiés ne sont pas toujours identifiés ni priorisés efficacement.")
        self.icp = make_icp(self.product)
        self.campaign = make_campaign(self.product, icp=self.icp)

    def test_rendered_email_never_overclaims_from_a_fit_only_signal(self):
        """Reproduit l'exemple exact de l'audit : un prospect n'a QUE le
        signal 'analytics_detected' (FIT) — l'e-mail final ne doit jamais
        affirmer qu'il cherche une solution d'analyse comportementale."""
        prospect = make_prospect()
        make_public_email(prospect)
        _fit_signal(prospect)

        brief = generate_agent_brief(prospect, icp=self.icp, product=self.product)
        campaign_prospect = CampaignProspect.objects.create(
            campaign=self.campaign, prospect=prospect, agent_brief=brief, status="selected",
        )

        subject, html, text = render_predictneed_email(campaign_prospect)

        for rendered in (subject, html, text):
            violations = assert_no_overclaiming(rendered)
            self.assertEqual(violations, [], f"overclaiming trouvé dans : {rendered!r}")
        self.assertNotIn("analyse comportementale", text.lower())

    def test_rendered_email_can_cite_a_real_intent_signal(self):
        prospect = make_prospect()
        make_public_email(prospect)
        _intent_signal(prospect)

        brief = generate_agent_brief(prospect, icp=self.icp, product=self.product)
        campaign_prospect = CampaignProspect.objects.create(
            campaign=self.campaign, prospect=prospect, agent_brief=brief, status="selected",
        )

        subject, html, text = render_predictneed_email(campaign_prospect)
        for rendered in (subject, html, text):
            self.assertEqual(assert_no_overclaiming(rendered), [])
