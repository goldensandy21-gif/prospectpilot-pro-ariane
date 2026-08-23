"""Correctif audit — automatisation email (commit 8d7bee3 corrigé).

Couvre les 7 sections du correctif :
1) adoption sûre des campagnes existantes dans le Planning ;
2) verrou anti-doublon réellement branché aux 3 niveaux (sélection UI,
   inscription POST y compris forgé, pré-SMTP) ;
3) une seule version candidate rendue, réutilisée telle quelle pour le test
   ET l'envoi commercial (jamais un nouveau rendu) ;
4) planification déterministe lundi->vendredi, scheduled_date réellement
   respecté par le scheduler ;
5) IMAP : jamais de modification de \\Seen, idempotence par registre
   applicatif ;
6) retry/backoff SMTP, jamais une rafale de tentatives.
"""
import datetime
from email.message import Message
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import (
    AgentBrief,
    Campaign,
    CampaignProspect,
    EmailAutomationSettings,
    EmailSend,
    EmailSequence,
    EmailStep,
    EmailVariant,
    PlannedEmailContent,
    ProcessedInboundMessage,
)
from prospects.services.campaign_sequencing import advance_campaign_prospect
from prospects.services.email_automation import (
    adopt_campaign_into_planning,
    build_week_plan,
    clone_sequence_for_campaign,
    finalize_failed_send,
    has_prior_commercial_first_contact,
    normalize_planning_sequence,
    prepare_planned_content,
    send_test_email,
    smtp_retry_allowed,
    SMTP_BACKOFF_MINUTES,
    SMTP_MAX_ATTEMPTS,
    validate_planned_content,
)
from prospects.services.inbound_replies import poll_inbound_replies
from prospects.services.predictneed_email import send_predictneed_campaign_email

from .factories import make_campaign, make_compliance_profile, make_icp, make_product, make_prospect, make_public_email
from .test_mission8_email_automation import make_j0_j4_j8_j14_sequence, make_planning_campaign


def _validate_after_test(planned, user=None):
    """Section 4 (audit correctif final) : validate_planned_content() exige
    désormais un test réussi sur EXACTEMENT ce contenu avant d'autoriser la
    validation. Helper de test — emprunte le vrai chemin d'envoi (is_test
    envoyé vers l'adresse d'expédition du produit), pas un raccourci qui
    contournerait la vérification serveur elle-même."""
    campaign_prospect = planned.campaign_prospect
    send_test_email(campaign_prospect, planned, campaign_prospect.campaign.product.sender_email)
    planned.refresh_from_db()
    return validate_planned_content(planned, user)


def make_legacy_three_step_0_4_8_sequence(product, icp):
    """Reproduit EXACTEMENT campaign_sending.get_or_create_default_sequence()
    — delay_days stockés 0/4/8, dont le cumulatif RÉEL (delay_days relatif à
    l'étape précédente) est 0/4/12, pas 0/4/8 (c'est précisément le bug visé
    par la section 1 : le J8 legacy tombe en réalité à J12)."""
    sequence = EmailSequence.objects.create(product=product, icp=icp, name="Legacy 0/4/8 (reconstruction réelle)")
    step1 = EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, name="Premier contact", channel="email")
    EmailVariant.objects.create(step=step1, name="Standard", subject_template="{{ company_name }} — observation", cta_type="simulator")
    step2 = EmailStep.objects.create(sequence=sequence, order=2, delay_days=4, name="Rappel court", channel="email")
    EmailVariant.objects.create(step=step2, name="Standard", subject_template="{{ company_name }} — comportement des visiteurs", cta_type="simulator")
    step3 = EmailStep.objects.create(sequence=sequence, order=3, delay_days=8, name="Dernier message", channel="email")
    EmailVariant.objects.create(step=step3, name="Standard", subject_template="{{ company_name }} — comportement des visiteurs", cta_type="simulator")
    return sequence


def make_legacy_single_step_campaign(product, icp, subject="J0 personnalisé — {{ company_name }}"):
    """Reproduit la forme réelle des Campaign #2-6 : créées AVANT
    planning_managed, une seule étape J0 avec un sujet déjà personnalisé,
    total_limit=1, validated_at déjà renseigné par l'ancien flux."""
    sequence = EmailSequence.objects.create(product=product, icp=icp, name="Legacy J0 only")
    step1 = EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, name="Premier contact", channel="email")
    EmailVariant.objects.create(step=step1, name="J0", subject_template=subject, cta_type="simulator")

    old_validated_at = timezone.now() - datetime.timedelta(days=3)
    campaign = Campaign.objects.create(
        name="Micro-vague legacy", product=product, icp=icp, sequence=sequence,
        status="ready", validated_at=old_validated_at, daily_send_limit=1, total_limit=1,
        planning_managed=False,
    )
    return campaign, sequence, step1


class AdoptionOfExistingCampaignsTests(TestCase):
    """Section 1 — Campaign #2-6 (forme reconstruite ici)."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)

    def test_legacy_campaign_adopted_without_sending(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        prospect = make_prospect()
        make_public_email(prospect)
        brief = AgentBrief.objects.create(prospect=prospect, product=self.product, icp=self.icp, detected_need="x")
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, agent_brief=brief, status="ready_to_contact")

        outbox_before = len(mail.outbox)
        adopt_campaign_into_planning(campaign)

        self.assertEqual(len(mail.outbox), outbox_before)
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member).count(), 0)
        self.assertTrue(campaign.planning_managed)

    def test_old_validated_at_is_invalidated_not_treated_as_new_approval(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        old_validated_at = campaign.validated_at
        self.assertIsNotNone(old_validated_at)

        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()

        self.assertEqual(campaign.status, "draft")
        self.assertIsNone(campaign.validated_at)
        self.assertIsNone(campaign.validated_by)
        self.assertFalse(campaign.is_sendable)

    def test_scheduler_does_nothing_for_freshly_adopted_campaign(self):
        """L'ancienne validated_at n'autorise rien : sans nouvelle validation
        humaine explicite, le scheduler ne peut toujours rien envoyer."""
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        prospect = make_prospect()
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")

        adopt_campaign_into_planning(campaign)

        result = advance_campaign_prospect(member.pk)
        self.assertEqual(result["action"], "not_sendable")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member).count(), 0)

    def test_total_limit_1_no_longer_blocks_j4_after_adoption_and_new_validation(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        prospect = make_prospect()
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")

        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()
        self.assertGreaterEqual(campaign.total_limit, 4)

        # Nouvelle validation humaine explicite (simulée) pour J0.
        step1 = campaign.sequence.steps.get(order=1)
        planned_j0 = prepare_planned_content(member, step1, timezone.now().date())
        _validate_after_test(planned_j0, None)
        campaign.status = "ready"
        campaign.validated_at = timezone.now()
        campaign.save(update_fields=["status", "validated_at"])

        result_j0 = advance_campaign_prospect(member.pk)
        self.assertEqual(result_j0["action"], "email")

        # J4 : sans le correctif de total_limit, la campagne (total_limit=1
        # à l'origine) aurait bloqué ici. `now` avancé de quelques jours pour
        # rester réaliste (J4 n'a jamais lieu le même jour calendaire que J0,
        # et daily_send_limit=1 ne doit pas être confondu avec total_limit).
        later = timezone.now() + datetime.timedelta(days=5)
        step2 = campaign.sequence.steps.get(order=2)
        planned_j4 = prepare_planned_content(member, step2, later.date())
        _validate_after_test(planned_j4, None)
        member.current_step_started_at = timezone.now() - datetime.timedelta(days=10)
        member.save(update_fields=["current_step_started_at"])

        result_j4 = advance_campaign_prospect(member.pk, now=later)
        self.assertEqual(result_j4["action"], "email")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False, status="sent").count(), 2)

    def test_imported_sequence_has_exactly_four_steps_with_correct_delays(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        adopt_campaign_into_planning(campaign)
        campaign.refresh_from_db()

        steps = list(campaign.sequence.steps.order_by("order"))
        self.assertEqual(len(steps), 4)
        self.assertEqual([s.order for s in steps], [1, 2, 3, 4])
        self.assertEqual([s.delay_days for s in steps], [0, 4, 4, 6])

    def test_j0_personalized_subject_never_overwritten_by_adoption(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(
            self.product, self.icp, subject="ENTREPRISE UNIQUE — sujet déjà personnalisé à la main",
        )
        original_variant = step1.variants.first()
        original_subject = original_variant.subject_template
        original_step_pk = step1.pk
        original_variant_pk = original_variant.pk

        adopt_campaign_into_planning(campaign)

        step1.refresh_from_db()
        original_variant.refresh_from_db()
        self.assertEqual(step1.pk, original_step_pk)
        self.assertEqual(original_variant.pk, original_variant_pk)
        self.assertEqual(original_variant.subject_template, original_subject)

    def test_adoption_refused_and_nothing_changed_if_already_sent(self):
        campaign, sequence, step1 = make_legacy_single_step_campaign(self.product, self.icp)
        prospect = make_prospect()
        make_public_email(prospect)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")

        # Ce prospect a déjà reçu un premier contact commercial dans une AUTRE campagne réelle.
        other_campaign = make_planning_campaign(self.product, self.icp, name="Autre")
        other_step1 = other_campaign.sequence.steps.get(order=1)
        other_member = CampaignProspect.objects.create(campaign=other_campaign, prospect=prospect, status="ready_to_contact")
        planned = prepare_planned_content(other_member, other_step1, timezone.now().date())
        _validate_after_test(planned, None)
        advance_campaign_prospect(other_member.pk)
        self.assertTrue(has_prior_commercial_first_contact(prospect))

        with self.assertRaises(ValueError):
            adopt_campaign_into_planning(campaign)

        campaign.refresh_from_db()
        self.assertFalse(campaign.planning_managed)
        self.assertEqual(campaign.total_limit, 1)
        self.assertEqual(campaign.sequence.steps.count(), 1)

    def test_normalize_planning_sequence_adds_missing_steps_and_preserves_j0_subject(self):
        sequence = EmailSequence.objects.create(product=self.product, icp=self.icp, name="Partial")
        step1 = EmailStep.objects.create(sequence=sequence, order=1, delay_days=0, name="J0", channel="email")
        variant = EmailVariant.objects.create(step=step1, name="J0", subject_template="Custom J0 subject", cta_type="simulator")

        normalize_planning_sequence(sequence)

        self.assertEqual(sequence.steps.count(), 4)
        step1.refresh_from_db()
        variant.refresh_from_db()
        self.assertEqual(variant.subject_template, "Custom J0 subject")
        for order in (2, 3, 4):
            step = sequence.steps.get(order=order)
            self.assertTrue(step.variants.exists())

    def test_normalize_planning_sequence_fixes_real_legacy_0_4_8_delays_to_0_4_4_6(self):
        """Section 1 — la séquence legacy réelle (get_or_create_default_sequence)
        stocke delay_days 0/4/8, dont le cumulatif RÉEL est 0/4/12 (delay_days
        est relatif à l'étape précédente), pas 0/4/8. normalize_planning_sequence
        doit corriger l'étape 3 existante (8 -> 4) et ajouter la 4e étape
        (delay_days=6), jamais seulement ajouter ce qui manque."""
        sequence = make_legacy_three_step_0_4_8_sequence(self.product, self.icp)
        original_step1_pk = sequence.steps.get(order=1).pk
        original_variant1 = sequence.steps.get(order=1).variants.first()
        original_subject = original_variant1.subject_template

        normalize_planning_sequence(sequence)

        steps = list(sequence.steps.order_by("order"))
        self.assertEqual(len(steps), 4)
        self.assertEqual([s.order for s in steps], [1, 2, 3, 4])
        self.assertEqual([s.delay_days for s in steps], [0, 4, 4, 6])
        # J0 jamais touché (ni l'étape ni son sujet personnalisé).
        self.assertEqual(steps[0].pk, original_step1_pk)
        original_variant1.refresh_from_db()
        self.assertEqual(original_variant1.subject_template, original_subject)

    def test_clone_sequence_for_campaign_never_mutates_the_original(self):
        """Section 2 — deux campagnes partagent la même séquence legacy ;
        adopter la campagne A ne doit JAMAIS modifier la séquence utilisée
        par la campagne B, qui doit rester strictement byte pour byte
        inchangée (mêmes pk d'étapes/variants, mêmes delay_days, même
        sujet)."""
        shared_sequence = make_legacy_three_step_0_4_8_sequence(self.product, self.icp)
        campaign_a = Campaign.objects.create(
            name="Partagee A", product=self.product, icp=self.icp, sequence=shared_sequence,
            status="ready", validated_at=timezone.now() - datetime.timedelta(days=2),
            daily_send_limit=1, total_limit=1, planning_managed=False,
        )
        campaign_b = Campaign.objects.create(
            name="Partagee B", product=self.product, icp=self.icp, sequence=shared_sequence,
            status="ready", validated_at=timezone.now() - datetime.timedelta(days=2),
            daily_send_limit=1, total_limit=1, planning_managed=False,
        )
        b_steps_before = list(shared_sequence.steps.order_by("order").values("pk", "order", "delay_days"))
        b_variant_before = shared_sequence.steps.get(order=1).variants.first().subject_template

        adopt_campaign_into_planning(campaign_a)
        campaign_a.refresh_from_db()
        campaign_b.refresh_from_db()

        # A a bien reçu une séquence à part, jamais la séquence partagée elle-même.
        self.assertNotEqual(campaign_a.sequence_id, shared_sequence.pk)
        self.assertEqual(campaign_a.sequence.steps.count(), 4)

        # B référence toujours EXACTEMENT la même séquence, elle-même inchangée.
        self.assertEqual(campaign_b.sequence_id, shared_sequence.pk)
        shared_sequence.refresh_from_db()
        b_steps_after = list(shared_sequence.steps.order_by("order").values("pk", "order", "delay_days"))
        self.assertEqual(b_steps_before, b_steps_after)
        self.assertEqual(shared_sequence.steps.count(), 3)  # jamais complétée à 4
        self.assertEqual(shared_sequence.steps.get(order=1).variants.first().subject_template, b_variant_before)

    def test_clone_sequence_for_campaign_is_a_deep_copy(self):
        sequence = make_legacy_three_step_0_4_8_sequence(self.product, self.icp)
        campaign = Campaign.objects.create(
            name="Cible clone", product=self.product, icp=self.icp, sequence=sequence,
            status="draft", daily_send_limit=1, total_limit=1, planning_managed=False,
        )
        clone = clone_sequence_for_campaign(sequence, campaign)

        self.assertNotEqual(clone.pk, sequence.pk)
        self.assertEqual(clone.steps.count(), sequence.steps.count())
        for original_step in sequence.steps.all():
            cloned_step = clone.steps.get(order=original_step.order)
            self.assertNotEqual(cloned_step.pk, original_step.pk)
            self.assertEqual(cloned_step.delay_days, original_step.delay_days)
            self.assertEqual(
                cloned_step.variants.first().subject_template,
                original_step.variants.first().subject_template,
            )


class CentralizedAntiDuplicateIntegrationTests(TestCase):
    """Section 2 — vraies vues/formulaires, pas seulement les fonctions helper."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="tester", password="x")
        self.client = Client()
        self.client.force_login(self.user)

    def _sent_first_contact(self, prospect):
        campaign = make_planning_campaign(self.product, self.icp, name=f"Prior-{prospect.pk}")
        step1 = campaign.sequence.steps.get(order=1)
        member = CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")
        planned = prepare_planned_content(member, step1, timezone.now().date())
        _validate_after_test(planned, None)
        result = advance_campaign_prospect(member.pk)
        self.assertEqual(result["action"], "email")

    def test_selection_ui_excludes_already_contacted_prospect(self):
        contacted = make_prospect(name="Deja Contacte", selected_for_prospecting=True, outbound_eligible=True, predictneed_excluded=False)
        make_public_email(contacted, email="contacted@example.com")
        self._sent_first_contact(contacted)

        fresh = make_prospect(name="Tout Nouveau", siret="00000000011111", selected_for_prospecting=True, outbound_eligible=True, predictneed_excluded=False)
        make_public_email(fresh, email="fresh@example.com")

        response = self.client.get(reverse("campaign_create"), {"planning": "1", "grade": "A,B,C"})
        prospects_shown = list(response.context["prospects"])
        self.assertIn(fresh, prospects_shown)
        self.assertNotIn(contacted, prospects_shown)

    def test_forged_post_blocked_for_planning_managed_campaign(self):
        contacted = make_prospect(name="Deja Contacte 2", selected_for_prospecting=True, outbound_eligible=True, predictneed_excluded=False)
        make_public_email(contacted, email="contacted2@example.com")
        self._sent_first_contact(contacted)

        emails_before = len(mail.outbox)
        response = self.client.post(reverse("campaign_create"), {
            "name": "Campagne forgee", "product": self.product.pk, "icp": self.icp.pk,
            "objective": "x", "score_threshold": 50, "daily_send_limit": 30, "total_limit": 200,
            "planning_managed": "on",
            "selected": [str(contacted.pk)],
        })
        self.assertEqual(response.status_code, 302)
        new_campaign = Campaign.objects.filter(name="Campagne forgee").latest("id")
        self.assertTrue(new_campaign.planning_managed)
        self.assertFalse(CampaignProspect.objects.filter(campaign=new_campaign, prospect=contacted).exists())
        self.assertEqual(len(mail.outbox), emails_before)

    def test_forged_post_still_allowed_for_non_planning_campaign_no_regression(self):
        """Contrôle : le verrou est scopé à planning_managed — une campagne
        manuelle classique continue de fonctionner exactement comme avant."""
        contacted = make_prospect(name="Deja Contacte 3", selected_for_prospecting=True, outbound_eligible=True, predictneed_excluded=False)
        make_public_email(contacted, email="contacted3@example.com")
        self._sent_first_contact(contacted)

        response = self.client.post(reverse("campaign_create"), {
            "name": "Campagne manuelle", "product": self.product.pk, "icp": self.icp.pk,
            "objective": "x", "score_threshold": 50, "daily_send_limit": 30, "total_limit": 200,
            "selected": [str(contacted.pk)],
        })
        self.assertEqual(response.status_code, 302)
        new_campaign = Campaign.objects.filter(name="Campagne manuelle").latest("id")
        self.assertFalse(new_campaign.planning_managed)
        self.assertTrue(CampaignProspect.objects.filter(campaign=new_campaign, prospect=contacted).exists())

    def test_pre_smtp_guard_still_blocks_even_if_post_guard_bypassed(self):
        """Défense en profondeur : même si un CampaignProspect de premier
        contact existe déjà pour un prospect contacté (contournement
        hypothétique du garde POST), le verrou pré-SMTP bloque quand même."""
        contacted = make_prospect(name="Deja Contacte 4")
        make_public_email(contacted, email="contacted4@example.com")
        self._sent_first_contact(contacted)

        other_campaign = make_planning_campaign(self.product, self.icp, name="Bypass")
        step1 = other_campaign.sequence.steps.get(order=1)
        member = CampaignProspect.objects.create(campaign=other_campaign, prospect=contacted, status="ready_to_contact")
        planned = prepare_planned_content(member, step1, timezone.now().date())
        _validate_after_test(planned, None)

        result = advance_campaign_prospect(member.pk)
        self.assertEqual(result["action"], "blocked_duplicate_first_contact")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=member, is_test=False).count(), 0)


class SingleRenderWorkflowTests(TestCase):
    """Section 3 — le test reçu doit être le contenu validé, un seul rendu."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)

    def test_test_email_equals_validated_commercial_content(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        prepared_subject, prepared_html, prepared_text = planned.subject, planned.html_body, planned.text_body

        test_record = send_test_email(self.member, planned, "contact-predict@predictneed-ia.com")
        self.assertEqual(test_record.status, "sent")

        planned.refresh_from_db()
        ok, reason = validate_planned_content(planned, None)
        self.assertTrue(ok, reason)
        commercial_result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(commercial_result["action"], "email")
        commercial_record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")

        # Sujet/texte strictement identiques entre test et commercial, eux-
        # mêmes identiques à ce qui a été préparé — jamais régénérés. Le HTML
        # commercial est le HTML de test AVEC le pixel d'ouverture en plus
        # (section 5 : jamais de pixel dans le test, injecté seulement au
        # moment du vrai envoi).
        from prospects.services.predictneed_email import inject_open_pixel
        self.assertEqual(test_record.subject.removeprefix("[TEST] "), commercial_record.subject)
        self.assertEqual(test_record.text_body, commercial_record.text_body)
        self.assertEqual(test_record.html_body, prepared_html)
        self.assertEqual(commercial_record.html_body, inject_open_pixel(prepared_html, commercial_record.open_tracking_token))
        self.assertEqual(commercial_record.subject, prepared_subject)
        self.assertEqual(commercial_record.text_body, prepared_text)

    def test_validate_never_rewrites_the_prepared_content(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        subject_after_prepare = planned.subject
        html_after_prepare = planned.html_body
        text_after_prepare = planned.text_body

        _validate_after_test(planned, None)
        planned.refresh_from_db()

        self.assertEqual(planned.subject, subject_after_prepare)
        self.assertEqual(planned.html_body, html_after_prepare)
        self.assertEqual(planned.text_body, text_after_prepare)
        self.assertEqual(planned.status, "validated")

    def test_stale_after_prepare_blocks_validation_and_automatic_send(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        self.prospect.name = "Nom Change Apres Preparation"
        self.prospect.save(update_fields=["name"])

        validated, reason = validate_planned_content(planned, None)
        self.assertFalse(validated)
        self.assertEqual(reason, "stale")
        planned.refresh_from_db()
        self.assertEqual(planned.status, "stale")

        result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "blocked_awaiting_validation")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).count(), 0)

    def test_re_preparing_after_stale_allows_a_fresh_test_and_validation(self):
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        self.prospect.name = "Nom Change"
        self.prospect.save(update_fields=["name"])
        validate_planned_content(planned, None)  # -> stale

        # « Préparer » à nouveau : nouvelle version candidate.
        planned2 = prepare_planned_content(self.member, self.step1, timezone.now().date())
        self.assertEqual(planned2.status, "to_validate")
        self.assertIn("Nom Change", planned2.subject if "company_name" not in planned2.subject else planned2.html_body)

        validated, reason = _validate_after_test(planned2, None)
        self.assertTrue(validated, reason)


class RealWeeklySlotAssignmentTests(TestCase):
    """Section 4 — planning réel lundi->vendredi."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.settings_row = EmailAutomationSettings.objects.create(new_contacts_per_day=5, daily_total_limit=100)
        self.monday = datetime.datetime(2026, 8, 24, 8, 0, tzinfo=datetime.timezone.utc)  # lundi, dans la fenêtre

    def _make_new_contact(self, i):
        campaign = make_planning_campaign(self.product, self.icp, name=f"NewC{i}")
        prospect = make_prospect(name=f"Prospect{i}", siret=f"000000000{i:05d}")
        make_public_email(prospect, email=f"p{i}@example.com")
        return CampaignProspect.objects.create(campaign=campaign, prospect=prospect, status="ready_to_contact")

    def test_25_new_contacts_with_limit_5_spread_across_the_week(self):
        for i in range(25):
            self._make_new_contact(i)

        plan = build_week_plan(now=self.monday)
        by_date = {}
        for date, cp, step in plan:
            by_date.setdefault(date, 0)
            by_date[date] += 1

        expected_days = [datetime.date(2026, 8, 24) + datetime.timedelta(days=d) for d in range(5)]  # lun->ven
        self.assertEqual(sorted(by_date.keys()), expected_days)
        for d in expected_days:
            self.assertEqual(by_date[d], 5)

    def test_overflow_beyond_friday_rolls_to_next_monday(self):
        for i in range(30):
            self._make_new_contact(i)

        plan = build_week_plan(now=self.monday)
        by_date = {}
        for date, cp, step in plan:
            by_date.setdefault(date, 0)
            by_date[date] += 1

        next_monday = datetime.date(2026, 8, 31)
        self.assertIn(next_monday, by_date)
        self.assertEqual(by_date[next_monday], 5)  # 30 - 25 = 5 reportés

    def test_scheduled_date_is_a_real_constraint_email_cannot_leave_early(self):
        cp = self._make_new_contact(99)
        step1 = cp.campaign.sequence.steps.get(order=1)
        tuesday = datetime.date(2026, 8, 25)
        prepare_planned_content(cp, step1, tuesday)
        planned = PlannedEmailContent.objects.get(campaign_prospect=cp, email_step=step1)
        _validate_after_test(planned, None)

        result = advance_campaign_prospect(cp.pk, now=self.monday)
        self.assertEqual(result["action"], "deferred_not_yet_due")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=cp, is_test=False).count(), 0)

    def test_scheduled_date_reached_allows_send(self):
        cp = self._make_new_contact(98)
        step1 = cp.campaign.sequence.steps.get(order=1)
        prepare_planned_content(cp, step1, datetime.date(2026, 8, 24))
        planned = PlannedEmailContent.objects.get(campaign_prospect=cp, email_step=step1)
        _validate_after_test(planned, None)

        tuesday_now = datetime.datetime(2026, 8, 25, 8, 0, tzinfo=datetime.timezone.utc)
        result = advance_campaign_prospect(cp.pk, now=tuesday_now)
        self.assertEqual(result["action"], "email")

    def test_overdue_scheduled_email_sends_at_next_valid_run(self):
        """Retard d'exécution (scheduler resté silencieux) : un email dont
        la date programmée est déjà passée peut partir dès la prochaine
        exécution valide, jamais bloqué indéfiniment."""
        cp = self._make_new_contact(97)
        step1 = cp.campaign.sequence.steps.get(order=1)
        yesterday = datetime.date(2026, 8, 21)  # vendredi précédent
        prepare_planned_content(cp, step1, yesterday)
        planned = PlannedEmailContent.objects.get(campaign_prospect=cp, email_step=step1)
        _validate_after_test(planned, None)

        result = advance_campaign_prospect(cp.pk, now=self.monday)
        self.assertEqual(result["action"], "email")


class ImapNeverTouchesSeenAndIsIdempotentTests(TestCase):
    """Section 5."""

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
        _validate_after_test(planned, None)
        advance_campaign_prospect(self.member.pk)
        self.record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")

    def _raw_message_bytes(self, message_id_header=None, subject="Re: hello"):
        msg = Message()
        msg["In-Reply-To"] = self.record.message_id
        msg["From"] = "prospect@example.com"
        msg["Subject"] = subject
        if message_id_header:
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
    def test_select_is_readonly_and_fetch_uses_peek_never_rfc822(self, mock_imap_cls):
        conn = self._fake_conn([self._raw_message_bytes(message_id_header="<msg1@example.com>")])
        mock_imap_cls.return_value = conn

        poll_inbound_replies()

        conn.select.assert_called_once()
        args, kwargs = conn.select.call_args
        self.assertTrue(kwargs.get("readonly") is True or (len(args) > 1 and args[1] is True))
        conn.fetch.assert_called()
        for call in conn.fetch.call_args_list:
            self.assertIn("BODY.PEEK[]", call.args[1])
            self.assertNotIn("RFC822", call.args[1])

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_store_is_never_called(self, mock_imap_cls):
        conn = self._fake_conn([self._raw_message_bytes(message_id_header="<msg2@example.com>")])
        mock_imap_cls.return_value = conn

        poll_inbound_replies()

        conn.store.assert_not_called()

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_recognized_message_processed_exactly_once(self, mock_imap_cls):
        from prospects.models import ContactLog, EngagementEvent

        conn = self._fake_conn([self._raw_message_bytes(message_id_header="<msg3@example.com>")])
        mock_imap_cls.return_value = conn

        result = poll_inbound_replies()

        self.assertEqual(result["action"], "polled")
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 1)
        self.assertEqual(EngagementEvent.objects.filter(event_type="email_replied", prospect=self.prospect).count(), 1)
        self.assertEqual(ProcessedInboundMessage.objects.count(), 1)

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_double_polling_same_message_produces_single_contactlog(self, mock_imap_cls):
        from prospects.models import ContactLog

        raw = self._raw_message_bytes(message_id_header="<msg4@example.com>")

        mock_imap_cls.return_value = self._fake_conn([raw])
        poll_inbound_replies()

        # « Redémarrage » simulé : nouvelle connexion, même message toujours présent (jamais marqué \Seen).
        mock_imap_cls.return_value = self._fake_conn([raw])
        result2 = poll_inbound_replies()

        self.assertEqual(result2["results"][0]["action"], "already_processed")
        self.assertEqual(ContactLog.objects.filter(prospect=self.prospect, outcome="replied").count(), 1)
        self.assertEqual(ProcessedInboundMessage.objects.count(), 1)

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_unrecognized_message_registered_and_never_reprocessed(self, mock_imap_cls):
        msg = Message()
        msg["From"] = "personne@inconnue.example"
        msg["Subject"] = "Sans rapport"
        msg["Message-ID"] = "<msg5@example.com>"
        raw = msg.as_bytes()

        mock_imap_cls.return_value = self._fake_conn([raw])
        poll_inbound_replies()
        mock_imap_cls.return_value = self._fake_conn([raw])
        result2 = poll_inbound_replies()

        self.assertEqual(result2["results"][0]["action"], "already_processed")
        self.assertEqual(ProcessedInboundMessage.objects.count(), 1)

    @mock.patch.dict("os.environ", {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    @mock.patch("prospects.services.inbound_replies.imaplib.IMAP4_SSL")
    def test_message_without_message_id_header_gets_synthetic_identity_and_is_idempotent(self, mock_imap_cls):
        raw = self._raw_message_bytes(message_id_header=None)  # pas de Message-ID
        mock_imap_cls.return_value = self._fake_conn([raw])
        poll_inbound_replies()
        mock_imap_cls.return_value = self._fake_conn([raw])
        result2 = poll_inbound_replies()

        self.assertEqual(result2["results"][0]["action"], "already_processed")
        self.assertEqual(ProcessedInboundMessage.objects.count(), 1)


class SMTPRetryBackoffTests(TestCase):
    """Section 6."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="ready_to_contact")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        _validate_after_test(planned, None)

    def test_first_failure_sets_backoff_next_retry_at(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("SMTP down")):
            result = advance_campaign_prospect(self.member.pk)
        self.assertEqual(result["action"], "email_failed")
        record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
        self.assertEqual(record.status, "failed")
        self.assertIsNotNone(record.next_retry_at)
        expected_min = timezone.now() + datetime.timedelta(minutes=SMTP_BACKOFF_MINUTES[0] - 1)
        self.assertGreater(record.next_retry_at, expected_min)

    def test_retry_not_attempted_before_backoff_due(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("SMTP down")):
            advance_campaign_prospect(self.member.pk)

        # Le scheduler tourne toutes les 5 minutes : un appel immédiat ne doit rien retenter.
        result = advance_campaign_prospect(self.member.pk, now=timezone.now())
        self.assertEqual(result["action"], "blocked_retry_backoff")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).count(), 1)

    def test_retry_attempted_once_backoff_elapsed(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("SMTP down")):
            advance_campaign_prospect(self.member.pk)

        later = timezone.now() + datetime.timedelta(minutes=SMTP_BACKOFF_MINUTES[0] + 1)
        result = advance_campaign_prospect(self.member.pk, now=later)
        self.assertEqual(result["action"], "email")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).count(), 2)

    def test_permanent_failure_after_max_attempts_blocks_forever(self):
        now = timezone.now()
        for attempt in range(SMTP_MAX_ATTEMPTS):
            with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("SMTP down")):
                result = advance_campaign_prospect(self.member.pk, now=now)
            if attempt < SMTP_MAX_ATTEMPTS - 1:
                self.assertEqual(result["action"], "email_failed")
                last_record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
                now = last_record.next_retry_at + datetime.timedelta(minutes=1)

        last_record = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")
        self.assertEqual(last_record.status, "permanently_failed")
        self.assertIsNone(last_record.next_retry_at)

        far_future = now + datetime.timedelta(days=365)
        result = advance_campaign_prospect(self.member.pk, now=far_future)
        self.assertEqual(result["action"], "blocked_permanent_failure")
        self.assertEqual(EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).count(), SMTP_MAX_ATTEMPTS)

    def test_failure_history_is_preserved_never_overwritten(self):
        now = timezone.now()
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("first failure")):
            advance_campaign_prospect(self.member.pk, now=now)
        first = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).latest("id")

        later = first.next_retry_at + datetime.timedelta(minutes=1)
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("second failure")):
            advance_campaign_prospect(self.member.pk, now=later)

        all_records = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).order_by("id")
        self.assertEqual(all_records.count(), 2)
        self.assertEqual(all_records[0].error, "first failure")
        self.assertEqual(all_records[1].error, "second failure")

    def test_scheduler_tick_every_5_minutes_does_not_burst_retry(self):
        """Le scheduler tourne toutes les 5 minutes : un échec ne doit
        jamais provoquer une nouvelle tentative à chaque tick avant
        l'échéance du backoff."""
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("down")):
            advance_campaign_prospect(self.member.pk)

        now = timezone.now()
        for _ in range(3):  # 3 ticks de 5 minutes = 15 minutes, backoff initial = 5 minutes -> le 3e devrait passer
            now += datetime.timedelta(minutes=5)
            result = advance_campaign_prospect(self.member.pk, now=now)
        # Après ~15 minutes (>= 5 min de backoff), une nouvelle tentative doit avoir eu lieu au plus une fois.
        attempts = EmailSend.objects.filter(campaign_prospect=self.member, is_test=False).count()
        self.assertLessEqual(attempts, 2)
        self.assertGreaterEqual(attempts, 2)
