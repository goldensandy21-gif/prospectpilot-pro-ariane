"""Audit correctif final — automatisation email (commit e384875 corrigé).

Couvre les points de la section finale non déjà exercés dans
test_mission8_correctif_audit.py (sections 1/2/4/5 y sont mises à jour) :

3) une NOUVELLE campagne Planning sans séquence explicite reçoit toujours
   directement 4 étapes J0/J4/J8/J14 (jamais la séquence legacy 0/4/8) ;
6) J0/J4/J8/J14 ont RÉELLEMENT 4 corps différents ;
7) idempotence IMAP résistante à un crash entre « réclamation » et fin de
   traitement d'un message (ProcessedInboundMessage.status).
"""
from email.message import Message
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import (
    AgentBrief,
    Campaign,
    CampaignProspect,
    ContactLog,
    EmailSequence,
    EngagementEvent,
    ProcessedInboundMessage,
)
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.email_automation import prepare_planned_content, send_test_email, validate_planned_content
from prospects.services.inbound_replies import poll_inbound_replies
from prospects.services.predictneed_email import render_predictneed_email

from .factories import make_compliance_profile, make_icp, make_product, make_prospect, make_public_email
from .test_mission8_correctif_audit import _validate_after_test
from .test_mission8_email_automation import make_planning_campaign


class NewPlanningCampaignGetsFourStepsTests(TestCase):
    """Section 3 — vraie vue, pas seulement la fonction helper."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="tester3", password="x")
        self.client = Client()
        self.client.force_login(self.user)

    def test_new_planning_campaign_without_explicit_sequence_gets_exactly_four_steps(self):
        prospect = make_prospect(
            name="Nouveau Planning", selected_for_prospecting=True, outbound_eligible=True, predictneed_excluded=False,
        )
        make_public_email(prospect)

        response = self.client.post(reverse("campaign_create"), {
            "name": "Nouvelle campagne planning", "product": self.product.pk, "icp": self.icp.pk,
            "objective": "x", "score_threshold": 50, "daily_send_limit": 30, "total_limit": 200,
            "planning_managed": "on",
            "selected": [str(prospect.pk)],
            # "sequence" volontairement absent : c'est exactement le cas visé.
        })
        self.assertEqual(response.status_code, 302)

        campaign = Campaign.objects.filter(name="Nouvelle campagne planning").latest("id")
        self.assertTrue(campaign.planning_managed)
        self.assertIsNotNone(campaign.sequence)

        steps = list(campaign.sequence.steps.order_by("order"))
        self.assertEqual(len(steps), 4)
        self.assertEqual([s.order for s in steps], [1, 2, 3, 4])
        self.assertEqual([s.delay_days for s in steps], [0, 4, 4, 6])
        for step in steps:
            self.assertTrue(step.variants.exists())

    def test_manual_campaign_without_explicit_sequence_still_gets_legacy_default(self):
        """Contrôle : une campagne manuelle (comportement historique, non
        planning_managed) continue de recevoir get_or_create_default_sequence()
        (3 étapes 0/4/8) — rien ne change pour elle."""
        prospect = make_prospect(
            name="Manuelle Sans Sequence", selected_for_prospecting=True, outbound_eligible=True, predictneed_excluded=False,
        )
        make_public_email(prospect)

        response = self.client.post(reverse("campaign_create"), {
            "name": "Campagne manuelle sans sequence", "product": self.product.pk, "icp": self.icp.pk,
            "objective": "x", "score_threshold": 50, "daily_send_limit": 30, "total_limit": 200,
            "selected": [str(prospect.pk)],
        })
        self.assertEqual(response.status_code, 302)

        campaign = Campaign.objects.filter(name="Campagne manuelle sans sequence").latest("id")
        self.assertFalse(campaign.planning_managed)
        steps = list(campaign.sequence.steps.order_by("order"))
        self.assertEqual(len(steps), 3)
        self.assertEqual([s.delay_days for s in steps], [0, 4, 8])

    def test_two_planning_campaigns_created_this_way_do_not_share_a_mutated_sequence(self):
        """La séquence Planning par défaut est dédiée/normalisée une seule
        fois (get_or_create) — deux campagnes créées ainsi partagent la même
        séquence déjà canonique (0/4/4/6), jamais une variante corrompue par
        une double normalisation."""
        for i in range(2):
            prospect = make_prospect(name=f"P{i}", siret=f"0000000000{i:04d}", selected_for_prospecting=True, outbound_eligible=True, predictneed_excluded=False)
            make_public_email(prospect, email=f"p{i}@example.com")
            self.client.post(reverse("campaign_create"), {
                "name": f"Camp planning {i}", "product": self.product.pk, "icp": self.icp.pk,
                "objective": "x", "score_threshold": 50, "daily_send_limit": 30, "total_limit": 200,
                "planning_managed": "on",
                "selected": [str(prospect.pk)],
            })
        campaigns = Campaign.objects.filter(name__startswith="Camp planning")
        sequence_ids = {c.sequence_id for c in campaigns}
        self.assertEqual(len(sequence_ids), 1)
        shared_sequence = EmailSequence.objects.get(pk=sequence_ids.pop())
        self.assertEqual(shared_sequence.steps.count(), 4)
        self.assertEqual([s.delay_days for s in shared_sequence.steps.order_by("order")], [0, 4, 4, 6])


class FourDistinctEmailBodiesTests(TestCase):
    """Section 6 — J0/J4/J8/J14 ont réellement 4 corps différents."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        brief = AgentBrief.objects.create(
            prospect=self.prospect, product=self.product, icp=self.icp,
            detected_need="Le site laisse penser à un besoin de qualification plus fine des visiteurs.",
            relevant_signals=[{"label": "Page tarifs visitée trois fois en une semaine"}],
        )
        self.member = CampaignProspect.objects.create(
            campaign=self.campaign, prospect=self.prospect, agent_brief=brief, status="ready_to_contact",
        )
        self.steps = list(self.campaign.sequence.steps.order_by("order"))

    def _render(self, step):
        variant = step.variants.first()
        return render_predictneed_email(self.member, step, variant)

    def test_four_bodies_are_not_identical(self):
        bodies = [self._render(step) for step in self.steps]
        html_bodies = [html for _subject, html, _text in bodies]
        text_bodies = [text for _subject, _html, text in bodies]
        self.assertEqual(len(set(html_bodies)), 4, "les 4 corps HTML doivent être distincts")
        self.assertEqual(len(set(text_bodies)), 4, "les 4 corps texte doivent être distincts")

    def test_j0_is_the_longest_full_template_with_three_benefit_blocks(self):
        _subject, html_j0, _text = self._render(self.steps[0])
        self.assertIn("Comportements observés", html_j0)
        self.assertIn("Plus de clarté", html_j0)
        self.assertIn("Actions recommandées", html_j0)

    def test_j4_is_a_short_recall_without_the_three_benefit_blocks(self):
        _subject, html_j0, _text = self._render(self.steps[0])
        _subject, html_j4, _text = self._render(self.steps[1])
        self.assertNotIn("Comportements observés", html_j4)
        self.assertNotIn("Plus de clarté", html_j4)
        self.assertNotIn("Actions recommandées", html_j4)
        self.assertLess(len(html_j4), len(html_j0))

    def test_j8_uses_a_new_hedged_angle_not_j0s_headline(self):
        _subject, html_j8, text_j8 = self._render(self.steps[2])
        self.assertIn("point de friction possible", text_j8 + html_j8)
        self.assertIn("sans certitude", text_j8 + html_j8)
        self.assertNotIn("Comportements observés", html_j8)

    def test_j14_is_short_and_explicitly_states_it_is_the_last_message(self):
        _subject, html_j0, _text = self._render(self.steps[0])
        _subject, html_j14, text_j14 = self._render(self.steps[3])
        self.assertIn("dernier message automatique", text_j14.lower())
        self.assertLess(len(html_j14), len(html_j0))

    def test_all_four_always_keep_compliance_footer(self):
        for step in self.steps:
            _subject, html, text = self._render(step)
            self.assertIn("Se désabonner", html)
            self.assertIn("Se désabonner", text)

    def test_subjects_also_differ_across_steps(self):
        subjects = [self._render(step)[0] for step in self.steps]
        self.assertEqual(len(set(subjects)), 4)


class ImapCrashResilienceTests(TestCase):
    """Section 7 — un crash entre « réclamation » et fin de traitement laisse
    le message réessayable, jamais perdu ni bloqué à jamais."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect, email="prospect@example.com")
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        step1 = self.campaign.sequence.steps.get(order=1)
        planned = prepare_planned_content(self.member, step1, timezone.now().date())
        _validate_after_test(planned)
        advance_campaign_prospect(self.member.pk)
        from prospects.models import EmailSend
        self.record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")

    def _raw_message_bytes(self, message_id_header="<crash-msg@example.com>", subject="Re: hello"):
        msg = Message()
        msg["In-Reply-To"] = self.record.message_id
        msg["From"] = "prospect@example.com"
        msg["Subject"] = subject
        msg["Message-ID"] = message_id_header
        return msg.as_bytes()

    def _fake_conn(self, raw_messages):
        conn = mock.MagicMock()
        conn.search.return_value = ("OK", [b" ".join(str(i + 1).encode() for i in range(len(raw_messages)))])
        conn.fetch.side_effect = [
            ("OK", [(f"{i+1} (BODY.PEEK[] {{{len(raw)}}}".encode(), raw)]) for i, raw in enumerate(raw_messages)
        ]
        return conn

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_message_left_in_processing_state_by_a_crash_is_resumed_next_poll(self, mock_imap_cls):
        """Simule un crash EXACTEMENT entre la réclamation (passage à
        `processing`) et la fin du traitement — jamais atteint `processed`
        — puis vérifie qu'un poll ultérieur reprend et termine le
        traitement (ContactLog/EngagementEvent bien créés), au lieu de
        rester bloqué indéfiniment."""
        raw = self._raw_message_bytes()

        # Pré-existant : registre déjà dans l'état laissé par un crash
        # antérieur (réclamé, jamais terminé).
        ProcessedInboundMessage.objects.create(message_id="<crash-msg@example.com>", status="processing")
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 0)

        mock_imap_cls.return_value = self._fake_conn([raw])
        result = poll_inbound_replies()

        self.assertEqual(result["results"][0]["action"], "matched")
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 1)
        self.assertEqual(EngagementEvent.objects.filter(event_type="email_replied", prospect=self.prospect).count(), 1)
        registry_row = ProcessedInboundMessage.objects.get(message_id="<crash-msg@example.com>")
        self.assertEqual(registry_row.status, "processed")

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_message_left_in_failed_state_is_also_resumed(self, mock_imap_cls):
        raw = self._raw_message_bytes(message_id_header="<crash-msg-2@example.com>")
        ProcessedInboundMessage.objects.create(message_id="<crash-msg-2@example.com>", status="failed", result="error: boom")

        mock_imap_cls.return_value = self._fake_conn([raw])
        result = poll_inbound_replies()

        self.assertEqual(result["results"][0]["action"], "matched")
        registry_row = ProcessedInboundMessage.objects.get(message_id="<crash-msg-2@example.com>")
        self.assertEqual(registry_row.status, "processed")

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_already_processed_message_is_never_reprocessed_even_after_a_later_crash_elsewhere(self, mock_imap_cls):
        raw = self._raw_message_bytes(message_id_header="<already-done@example.com>")
        ProcessedInboundMessage.objects.create(message_id="<already-done@example.com>", status="processed", result="matched")

        mock_imap_cls.return_value = self._fake_conn([raw])
        result = poll_inbound_replies()

        self.assertEqual(result["results"][0]["action"], "already_processed")
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 0)

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_engagement_event_idempotency_key_prevents_duplicate_even_if_registry_row_missing(self, mock_imap_cls):
        """Ceinture ET bretelles (section 7) : même si le registre
        applicatif était absent/incohérent, la création de l'EngagementEvent
        elle-même (idempotency_key unique) empêche tout doublon de
        ContactLog pour le même message."""
        from prospects.services.inbound_replies import process_inbound_message

        msg = Message()
        msg["In-Reply-To"] = self.record.message_id
        msg["From"] = "prospect@example.com"
        msg["Subject"] = "Re: hello"

        result1 = process_inbound_message(msg, message_identity="<direct-call@example.com>")
        result2 = process_inbound_message(msg, message_identity="<direct-call@example.com>")

        self.assertEqual(result1["action"], "matched")
        self.assertEqual(result2["action"], "already_recorded")
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 1)
        self.assertEqual(EngagementEvent.objects.filter(event_type="email_replied", prospect=self.prospect).count(), 1)
