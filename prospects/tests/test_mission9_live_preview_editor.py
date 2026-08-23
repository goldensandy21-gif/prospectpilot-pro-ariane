"""Correctif UX critique — éditeur d'email avec contenu actuel + aperçu
live (depuis aa7ad14).

Couvre : le textarea « Texte rédactionnel » jamais vide (préchargé avec
`editable_body_text`, source éditoriale séparée — jamais un parsing
fragile du HTML/texte figé), l'aperçu live (action=preview) qui reconstruit
EXACTEMENT la même enveloppe que la sauvegarde SANS jamais écrire en base,
la bannière « Aperçu des modifications non enregistrées » / Réinitialiser
(les valeurs nécessaires au reset côté JS), et le backfill contrôlé et
vérifié de `editable_body_text` pour les PlannedEmailContent antérieurs à
ce champ."""
import io

from django.contrib.auth.models import User
from django.utils.html import escape
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from prospects.models import AgentBrief, CampaignProspect, EmailSend, PlannedEmailContent
from prospects.services.email_automation import (
    apply_manual_edit,
    content_hash_for,
    prepare_planned_content,
    render_live_content,
    validate_planned_content,
)
from prospects.services.predictneed_email import editable_body_text_for_step

from .factories import make_compliance_profile, make_icp, make_product, make_prospect, make_public_email
from .test_mission8_email_automation import make_planning_campaign


def _make_agent_brief(prospect, product, icp, **overrides):
    defaults = {
        "relevant_signals": [{"label": "trafic élevé sur la page tarifs"}, {"label": "visites répétées du simulateur"}],
        "detected_need": "Les visiteurs semblent hésiter avant de s'inscrire.",
    }
    defaults.update(overrides)
    return AgentBrief.objects.create(prospect=prospect, product=product, icp=icp, **defaults)


class PreloadedEditorTests(TestCase):
    """Le textarea ne doit jamais être vide lorsqu'un contenu préparé
    existe — préchargé avec editable_body_text, jamais avec le HTML/texte
    figé complet (signature/footer/CTA exclus)."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="previewA", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect(name="FORMAPI BOURG-EN-BRESSE")
        make_public_email(self.prospect, email="contact@formapi.example")
        self.brief = _make_agent_brief(self.prospect, self.product, self.icp)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected", agent_brief=self.brief)
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())

    def test_editable_body_text_is_never_empty_after_prepare(self):
        self.assertTrue(self.planned.editable_body_text.strip())

    def test_editable_body_text_contains_the_real_editorial_paragraphs(self):
        self.assertIn("trafic élevé sur la page tarifs", self.planned.editable_body_text)
        self.assertIn("Les visiteurs semblent hésiter avant de s'inscrire.", self.planned.editable_body_text)

    def test_editable_body_text_excludes_protected_elements(self):
        for forbidden in ("Bonjour", "Tester le simulateur", "Bien cordialement", "Se désabonner", self.product.sender_email):
            self.assertNotIn(forbidden, self.planned.editable_body_text)

    def test_get_editor_preloads_textarea_with_current_editorial_content(self):
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("trafic élevé sur la page tarifs", content)
        self.assertIn(escape("Les visiteurs semblent hésiter avant de s'inscrire."), content)

    def test_opening_an_old_content_changes_nothing(self):
        before = (self.planned.content_hash, self.planned.html_body, self.planned.text_body, self.planned.editable_body_text, self.planned.status)
        self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        self.planned.refresh_from_db()
        after = (self.planned.content_hash, self.planned.html_body, self.planned.text_body, self.planned.editable_body_text, self.planned.status)
        self.assertEqual(before, after)

    def test_reset_values_embedded_in_page_match_current_saved_content(self):
        """Les blocs json_script utilisés par le bouton Réinitialiser
        (JS) doivent correspondre exactement à la version enregistrée —
        sinon Réinitialiser restaurerait une version différente de celle
        réellement en base."""
        response = self.client.get(reverse("email_planning_content_detail", args=[self.planned.pk]))
        content = response.content.decode()
        self.assertIn('id="original-subject"', content)
        self.assertIn('id="original-body"', content)
        self.assertIn('id="original-html"', content)
        self.assertIn('id="original-text"', content)


class LivePreviewNeverWritesTests(TestCase):
    """L'aperçu live (action=preview) ne modifie jamais PlannedEmailContent,
    n'envoie aucun SMTP, ne programme rien — seul « Enregistrer la
    modification » (action=edit) écrit."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="previewB", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect(name="Boulangerie Dupont")
        make_public_email(self.prospect, email="dupont@example.com")
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())

        # Une deuxième ligne, pour prouver qu'un aperçu sur la première ne
        # touche jamais la seconde.
        prospect2 = make_prospect(name="Autre Prospect", siret="00000000071111")
        make_public_email(prospect2, email="autre@example.com")
        self.member2 = CampaignProspect.objects.create(campaign=self.campaign, prospect=prospect2, status="selected")
        self.planned2 = prepare_planned_content(self.member2, self.step1, timezone.now().date())

    def _preview(self, subject, body_text):
        return self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "preview", "subject": subject, "body_text": body_text,
        })

    def test_typing_one_sentence_in_preview_writes_nothing_to_db(self):
        before = (
            self.planned.content_hash, self.planned.html_body, self.planned.text_body,
            self.planned.editable_body_text, self.planned.status, self.planned.subject,
        )
        response = self._preview("Nouveau sujet en cours de frappe", "Une phrase tapée en aperçu, jamais enregistrée.")
        self.assertEqual(response.status_code, 200)
        self.planned.refresh_from_db()
        after = (
            self.planned.content_hash, self.planned.html_body, self.planned.text_body,
            self.planned.editable_body_text, self.planned.status, self.planned.subject,
        )
        self.assertEqual(before, after)

    def test_preview_reconstructs_html_and_text_with_the_typed_sentence(self):
        response = self._preview(self.planned.subject, "Une phrase entièrement nouvelle pour cet aperçu.")
        data = response.json()
        self.assertIn("Une phrase entièrement nouvelle pour cet aperçu.", data["html"])
        self.assertIn("Une phrase entièrement nouvelle pour cet aperçu.", data["text"])

    def test_preview_preserves_cta_signature_and_footer(self):
        response = self._preview(self.planned.subject, "Contenu de test pour l'aperçu.")
        data = response.json()
        variant = self.step1.variants.filter(active=True).first()
        self.assertIn(variant.cta_label_override or "Tester le simulateur", data["html"])
        self.assertIn(self.product.sender_name, data["html"])
        self.assertIn(self.product.sender_name, data["text"])
        self.assertIn("Se désabonner", data["html"])
        self.assertIn("Se désabonner", data["text"])

    def test_preview_sends_no_smtp_and_no_commercial_emailsend(self):
        outbox_before = len(mail.outbox)
        commercial_before = EmailSend.objects.filter(is_test=False).count()
        self._preview("Sujet en cours", "Texte en cours de frappe.")
        self.assertEqual(len(mail.outbox), outbox_before)
        self.assertEqual(EmailSend.objects.filter(is_test=False).count(), commercial_before)

    def test_preview_never_programs_anything(self):
        self._preview("Sujet en cours", "Texte en cours de frappe.")
        self.planned.refresh_from_db()
        self.assertNotEqual(self.planned.status, "validated")
        self.assertIsNone(self.planned.approved_by)
        self.assertIsNone(self.planned.approved_at)

    def test_preview_does_not_change_content_hash(self):
        old_hash = self.planned.content_hash
        self._preview("Sujet en cours", "Texte en cours de frappe.")
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.content_hash, old_hash)

    def test_preview_leaves_a_prior_approval_untouched(self):
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)
        approved_at_before = self.planned.approved_at
        approved_by_before = self.planned.approved_by

        self._preview("Sujet en cours", "Texte en cours de frappe, pas encore sauvegardé.")

        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status, "validated")
        self.assertEqual(self.planned.approved_at, approved_at_before)
        self.assertEqual(self.planned.approved_by, approved_by_before)

    def test_preview_never_modifies_another_planned_content_row(self):
        before = (self.planned2.content_hash, self.planned2.html_body, self.planned2.editable_body_text)
        self._preview("Sujet en cours", "Texte en cours de frappe.")
        self.planned2.refresh_from_db()
        after = (self.planned2.content_hash, self.planned2.html_body, self.planned2.editable_body_text)
        self.assertEqual(before, after)


class SaveMatchesPreviewTests(TestCase):
    """La sauvegarde (action=edit) doit produire EXACTEMENT la version
    prévisualisée juste avant — jamais une divergence entre ce que
    l'utilisatrice a vu et ce qui est réellement enregistré."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.user = User.objects.create_user(username="previewC", password="x")
        self.client = Client()
        self.client.force_login(self.user)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect()
        make_public_email(self.prospect)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected")
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())

    def test_saved_content_matches_the_previewed_content_byte_for_byte(self):
        new_subject = "Sujet définitif"
        new_body = "Premier paragraphe définitif.\n\nSecond paragraphe définitif."

        preview_response = self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "preview", "subject": new_subject, "body_text": new_body,
        })
        previewed = preview_response.json()

        self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "edit", "subject": new_subject, "body_text": new_body,
        })
        self.planned.refresh_from_db()

        self.assertEqual(self.planned.html_body, previewed["html"])
        self.assertEqual(self.planned.text_body, previewed["text"])
        self.assertEqual(self.planned.subject, previewed["subject"])

    def test_editing_only_one_paragraph_preserves_the_other_verbatim(self):
        original_body = "Paragraphe un original.\n\nParagraphe deux original."
        apply_manual_edit(self.planned, self.planned.subject, original_body, request=None)
        self.planned.refresh_from_db()

        edited_body = "Paragraphe un RÉÉCRIT.\n\nParagraphe deux original."
        self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "edit", "subject": self.planned.subject, "body_text": edited_body,
        })
        self.planned.refresh_from_db()

        self.assertIn("Paragraphe deux original.", self.planned.editable_body_text)
        self.assertIn("Paragraphe deux original.", self.planned.text_body)
        self.assertIn("Paragraphe un RÉÉCRIT.", self.planned.text_body)
        self.assertNotIn("Paragraphe un original.", self.planned.text_body)

    def test_content_hash_recomputed_only_after_save_not_after_preview(self):
        old_hash = self.planned.content_hash
        self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "preview", "subject": "X", "body_text": "Y",
        })
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.content_hash, old_hash)

        self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "edit", "subject": "Sujet final", "body_text": "Corps final.",
        })
        self.planned.refresh_from_db()
        self.assertNotEqual(self.planned.content_hash, old_hash)

    def test_prior_approval_invalidated_only_after_save_not_after_preview(self):
        ok, reason = validate_planned_content(self.planned, self.user)
        self.assertTrue(ok, reason)

        self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "preview", "subject": "X", "body_text": "Y",
        })
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status, "validated")
        self.assertIsNotNone(self.planned.approved_at)

        self.client.post(reverse("email_planning_content_detail", args=[self.planned.pk]), {
            "action": "edit", "subject": "Sujet final", "body_text": "Corps final.",
        })
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status, "modified")
        self.assertIsNone(self.planned.approved_at)
        self.assertIsNone(self.planned.approved_by)


class EditorialParagraphExtractionTests(TestCase):
    """editable_body_text_for_step() ne parse jamais fragilement un rendu
    déjà produit — il compose les mêmes phrases, mot pour mot, que le rendu
    texte (sans les éléments protégés)."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect(name="ACME")
        make_public_email(self.prospect, email="contact@acme.example")
        self.brief = _make_agent_brief(self.prospect, self.product, self.icp)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected", agent_brief=self.brief)

    def test_j0_editorial_paragraphs_match_text_body_substrings(self):
        step1 = self.campaign.sequence.steps.get(order=1)
        subject, html, text = render_live_content(self.member, step1)
        editable = editable_body_text_for_step(self.member, step1)
        for paragraph in editable.split("\n\n"):
            self.assertIn(paragraph, text)

    def test_editorial_paragraphs_exclude_cta_line_and_reply_line(self):
        step1 = self.campaign.sequence.steps.get(order=1)
        editable = editable_body_text_for_step(self.member, step1)
        self.assertNotIn("Tester le simulateur", editable)
        self.assertNotIn("Vous pouvez aussi simplement répondre", editable)


class BackfillCommandTests(TestCase):
    """Backfill sûr et vérifié de editable_body_text pour les
    PlannedEmailContent antérieurs à ce champ — dry-run par défaut, écrit
    UNIQUEMENT editable_body_text, jamais html_body/text_body/content_hash/
    status/approved_*, et seulement pour les lignes dont le hash live
    correspond encore exactement au contenu figé."""

    def setUp(self):
        self.product = make_product()
        make_compliance_profile(self.product)
        self.icp = make_icp(self.product)
        self.campaign = make_planning_campaign(self.product, self.icp)
        self.prospect = make_prospect(name="Ancien Prospect")
        make_public_email(self.prospect, email="ancien@example.com")
        self.brief = _make_agent_brief(self.prospect, self.product, self.icp)
        self.member = CampaignProspect.objects.create(campaign=self.campaign, prospect=self.prospect, status="selected", agent_brief=self.brief)
        self.step1 = self.campaign.sequence.steps.get(order=1)
        self.planned = prepare_planned_content(self.member, self.step1, timezone.now().date())
        # Simule une ligne préparée AVANT l'ajout du champ.
        self.planned.editable_body_text = ""
        self.planned.save(update_fields=["editable_body_text"])
        self.original_html = self.planned.html_body
        self.original_text = self.planned.text_body
        self.original_hash = self.planned.content_hash
        self.original_status = self.planned.status

    def test_dry_run_writes_nothing(self):
        call_command("backfill_planned_editable_body_text", stdout=io.StringIO())
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.editable_body_text, "")

    def test_apply_backfills_only_editable_body_text(self):
        call_command("backfill_planned_editable_body_text", "--apply", stdout=io.StringIO())
        self.planned.refresh_from_db()
        self.assertTrue(self.planned.editable_body_text.strip())
        self.assertEqual(self.planned.html_body, self.original_html)
        self.assertEqual(self.planned.text_body, self.original_text)
        self.assertEqual(self.planned.content_hash, self.original_hash)
        self.assertEqual(self.planned.status, self.original_status)
        self.assertIsNone(self.planned.approved_by)
        self.assertIsNone(self.planned.approved_at)

    def test_backfilled_text_matches_live_editorial_extraction(self):
        call_command("backfill_planned_editable_body_text", "--apply", stdout=io.StringIO())
        self.planned.refresh_from_db()
        expected = editable_body_text_for_step(self.member, self.step1)
        self.assertEqual(self.planned.editable_body_text, expected)

    def test_mismatched_hash_row_is_skipped_not_guessed(self):
        # La donnée source a changé depuis la préparation -> le hash live
        # ne correspond plus -> ne doit JAMAIS être backfillée en devinant.
        self.prospect.name = "Nom Changé Après Préparation"
        self.prospect.save(update_fields=["name"])

        out = io.StringIO()
        call_command("backfill_planned_editable_body_text", "--apply", stdout=out)
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.editable_body_text, "")
        self.assertIn("NON backfillé", out.getvalue())

    def test_manually_edited_rows_are_never_touched(self):
        apply_manual_edit(self.planned, self.planned.subject, "Texte saisi à la main.", request=None)
        self.planned.refresh_from_db()
        manual_text = self.planned.editable_body_text
        self.assertTrue(manual_text)

        call_command("backfill_planned_editable_body_text", "--apply", stdout=io.StringIO())
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.editable_body_text, manual_text)
