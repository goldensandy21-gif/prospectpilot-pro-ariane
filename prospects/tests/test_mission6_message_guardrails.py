from django.test import TestCase
from django.utils import timezone

from prospects.models import ProspectSignal
from prospects.services.campaign_sequencing import _build_linkedin_message
from prospects.services.message_guardrails import (
    assert_no_overclaiming,
    build_personalization_snippet,
    safe_personalization_for_signal,
)
from prospects.services.signals import signal_fingerprint
from prospects.tests.factories import make_prospect


def _signal(prospect, signal_type, category, signal_group, value="", label="", evidence="preuve"):
    return ProspectSignal.objects.create(
        prospect=prospect, signal_type=signal_type, category=category, signal_group=signal_group,
        source_kind="technology", label=label or signal_type, value=value, evidence=evidence,
        confidence=80, score_impact=5, positive=True, observed_at=timezone.now(),
        fingerprint=signal_fingerprint(signal_type, value, evidence),
    )


class SafePersonalizationForSignalTests(TestCase):
    def test_unlisted_signal_type_produces_no_phrase(self):
        prospect = make_prospect()
        signal = _signal(prospect, "some_unknown_signal_type", "risk", "risk")
        self.assertEqual(safe_personalization_for_signal(signal), "")

    def test_analytics_detected_never_becomes_a_behaviour_analytics_claim(self):
        """Le cas explicitement interdit par la mission : 'Google Analytics
        détecté' (FIT) ne doit jamais devenir une affirmation sur une
        intention d'analyse comportementale."""
        prospect = make_prospect()
        signal = _signal(
            prospect, "analytics_detected", "analytics", "fit",
            value="Google Analytics", label="Outils d'analytics détectés",
        )
        phrase = safe_personalization_for_signal(signal)
        self.assertIn("suivi d'audience", phrase)
        self.assertNotIn("analyse comportementale", phrase)
        self.assertEqual(assert_no_overclaiming(phrase), [])

    def test_fit_signal_phrase_never_claims_intent(self):
        prospect = make_prospect()
        signal = _signal(prospect, "crm_detected", "crm", "fit", value="HubSpot")
        phrase = safe_personalization_for_signal(signal)
        self.assertEqual(assert_no_overclaiming(phrase), [])

    def test_intent_signal_phrase_states_fact_not_intent(self):
        prospect = make_prospect()
        signal = _signal(prospect, "booking_detected", "conversion", "intent")
        phrase = safe_personalization_for_signal(signal)
        self.assertIn("votre site", phrase)
        self.assertEqual(assert_no_overclaiming(phrase), [])


class BuildPersonalizationSnippetTests(TestCase):
    def test_no_signals_returns_empty_list(self):
        prospect = make_prospect()
        self.assertEqual(build_personalization_snippet(prospect), [])

    def test_only_lists_known_signal_types(self):
        prospect = make_prospect()
        _signal(prospect, "analytics_detected", "analytics", "fit", value="Google Analytics")
        _signal(prospect, "some_unknown_signal_type", "risk", "risk")
        phrases = build_personalization_snippet(prospect)
        self.assertEqual(len(phrases), 1)

    def test_caps_at_max_signals(self):
        prospect = make_prospect()
        _signal(prospect, "analytics_detected", "analytics", "fit", value="GA")
        _signal(prospect, "crm_detected", "crm", "fit", value="HubSpot")
        _signal(prospect, "gtm_detected", "acquisition", "fit")
        phrases = build_personalization_snippet(prospect, max_signals=2)
        self.assertEqual(len(phrases), 2)


class AssertNoOverclaimingTests(TestCase):
    def test_detects_blocked_phrasing(self):
        violations = assert_no_overclaiming("Vous cherchez actuellement une solution d'analyse comportementale.")
        self.assertIn("vous cherchez", violations)

    def test_clean_factual_text_has_no_violations(self):
        violations = assert_no_overclaiming("Vous utilisez déjà des outils de suivi d'audience.")
        self.assertEqual(violations, [])


class LinkedinMessageGuardrailIntegrationTests(TestCase):
    def test_generated_linkedin_message_never_overclaims_from_analytics_signal(self):
        prospect = make_prospect()
        _signal(
            prospect, "analytics_detected", "analytics", "fit",
            value="Google Analytics", label="Outils d'analytics détectés",
        )
        message = _build_linkedin_message(prospect)
        self.assertEqual(assert_no_overclaiming(message), [])
        self.assertNotIn("analyse comportementale", message)

    def test_no_signals_produces_a_generic_but_safe_message(self):
        prospect = make_prospect()
        message = _build_linkedin_message(prospect)
        self.assertEqual(assert_no_overclaiming(message), [])
