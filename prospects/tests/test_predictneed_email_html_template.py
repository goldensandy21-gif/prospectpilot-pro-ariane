"""Nouveau template HTML des e-mails PredictNeed IA — rendu professionnel
(bandeau bleu marine, blocs bénéfices, CTA unique, footer réglementaire
restylé) tout en réutilisant exactement les mêmes données dynamiques et le
même mécanisme de conformité que l'ancien rendu minimal."""
from html import escape

from django.test import TestCase

from prospects.models import AgentBrief
from prospects.services.campaign_sending import get_or_create_default_sequence
from prospects.services.predictneed_email import render_predictneed_email
from prospects.services.suppression import is_suppressed
from prospects.services.tracking import build_tracking_url

from .factories import (
    make_campaign,
    make_campaign_prospect,
    make_compliance_profile,
    make_icp,
    make_product,
    make_prospect,
    make_public_email,
)


class NewTemplateBaseTests(TestCase):
    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.prospect = make_prospect(name="Formation Exemple")
        make_public_email(self.prospect)
        self.campaign = make_campaign(self.product, self.icp)
        self.member = make_campaign_prospect(self.campaign, self.prospect)
        self.sequence = get_or_create_default_sequence(self.product, self.icp)
        self.step = self.sequence.steps.order_by("order").first()
        self.variant = self.step.variants.first()
        self.brief = AgentBrief.objects.create(
            prospect=self.prospect, product=self.product, icp=self.icp,
            detected_need="Signaux observés chez ce prospect : formulaire d'inscription détecté.",
            relevant_signals=[{"label": "vous avez structuré un parcours d'inscription en ligne"}],
        )
        self.member.agent_brief = self.brief
        self.member.save(update_fields=["agent_brief"])

    def _render(self):
        return render_predictneed_email(self.member, self.step, self.variant)

    def test_predictneed_ia_branding_present(self):
        _, html, _ = self._render()
        self.assertIn("PredictNeed IA", html)

    def test_dynamic_observation_present(self):
        _, html, _ = self._render()
        self.assertIn("vous avez structuré un parcours d", html)
        self.assertIn("inscription en ligne", html)

    def test_dynamic_detected_problem_present(self):
        _, html, _ = self._render()
        self.assertIn("Signaux observés chez ce prospect", html)

    def test_value_proposition_present(self):
        _, html, _ = self._render()
        self.assertIn(escape(self.product.short_value_proposition), html)

    def test_exactly_one_cta_link(self):
        """Exactement un <a> réel vers le CTA — le href supplémentaire dans le
        bloc VML (<v:roundrect href="...">) est un repli Outlook standard,
        enveloppé dans un commentaire conditionnel, jamais un second lien réel."""
        _, html, _ = self._render()
        cta_url = build_tracking_url(self.member, cta_type="simulator", email_step=self.step, email_variant=self.variant)
        self.assertEqual(html.count(f'<a href="{escape(cta_url)}"'), 1)
        self.assertEqual(html.count(f'href="{escape(cta_url)}"'), 2)

    def test_cta_uses_prospectpilot_tracking_url_not_direct_predictneed(self):
        _, html, _ = self._render()
        self.assertIn(f"/t/{self.member.tracking_token}/", html)
        self.assertNotIn(f'href="{escape(self.product.simulator_url)}"', html)

    def test_reply_invitation_sentence_present_in_html_and_text(self):
        _, html, text = self._render()
        sentence = "Vous pouvez aussi simplement répondre à cet e-mail"
        self.assertIn(sentence, html)
        self.assertIn(sentence, text)

    def test_no_image_no_photo_in_html(self):
        _, html, _ = self._render()
        self.assertNotIn("<img", html.lower())

    def test_html_and_text_both_generated_and_substantive(self):
        _, html, text = self._render()
        self.assertGreater(len(html), 500)
        self.assertGreater(len(text), 100)

    def test_compliance_footer_present_html_and_text(self):
        _, html, text = self._render()
        self.assertIn("Se désabonner", html)
        self.assertIn("Se désabonner", text)
        self.assertIn(str(self.prospect.unsubscribe_token), html)
        self.assertIn(str(self.prospect.unsubscribe_token), text)

    def test_privacy_link_present_when_configured(self):
        _, html, _ = self._render()
        self.assertIn(f"/privacy/prospect/{self.prospect.unsubscribe_token}/", html)

    def test_legal_notice_link_present_when_configured(self):
        compliance = self.product.compliance_profile
        compliance.legal_notice_url = "https://predictneed-ia.example/mentions-legales"
        compliance.save(update_fields=["legal_notice_url"])
        _, html, _ = self._render()
        self.assertIn("https://predictneed-ia.example/mentions-legales", html)
        self.assertIn("Mentions légales", html)

    def test_table_based_bulletproof_structure(self):
        _, html, _ = self._render()
        self.assertIn('role="presentation"', html)
        self.assertIn("max-width:600px", html)
        self.assertNotIn("<script", html.lower())

    def test_suppression_behaviour_is_unaffected_by_template_change(self):
        self.assertFalse(is_suppressed(self.prospect.public_email, prospect=self.prospect))
        self.prospect.prospecting_allowed = False
        self.prospect.save(update_fields=["prospecting_allowed"])
        self.assertTrue(is_suppressed(self.prospect.public_email, prospect=self.prospect))


class NewTemplateEscapingTests(TestCase):
    def test_prospect_name_and_signal_label_are_html_escaped(self):
        product = make_product()
        make_compliance_profile(product)
        icp = make_icp(product)
        prospect = make_prospect(name='<script>alert("x")</script> & Co')
        make_public_email(prospect)
        campaign = make_campaign(product, icp)
        member = make_campaign_prospect(campaign, prospect)
        sequence = get_or_create_default_sequence(product, icp)
        step = sequence.steps.order_by("order").first()
        variant = step.variants.first()
        brief = AgentBrief.objects.create(
            prospect=prospect, product=product, icp=icp,
            detected_need="",
            relevant_signals=[{"label": '<img src=x onerror=alert(1)> observation'}],
        )
        member.agent_brief = brief
        member.save(update_fields=["agent_brief"])

        _, html, text = render_predictneed_email(member, step, variant)

        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x onerror", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp; Co", html)


class NewTemplateNoAgentBriefTests(TestCase):
    """Aucune donnée ne doit jamais être inventée quand l'AgentBrief est absent
    ou vide — le rendu reste valide, seulement moins personnalisé."""

    def test_renders_cleanly_without_agent_brief(self):
        product = make_product()
        make_compliance_profile(product)
        icp = make_icp(product)
        prospect = make_prospect()
        make_public_email(prospect)
        campaign = make_campaign(product, icp)
        member = make_campaign_prospect(campaign, prospect, agent_brief=None)
        sequence = get_or_create_default_sequence(product, icp)
        step = sequence.steps.order_by("order").first()
        variant = step.variants.first()

        subject, html, text = render_predictneed_email(member, step, variant)

        self.assertIn("Bonjour,", html)
        self.assertIn("PredictNeed IA", html)
        self.assertIn("Se désabonner", html)
        self.assertGreater(len(html), 300)
