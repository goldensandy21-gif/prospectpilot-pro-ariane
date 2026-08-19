from django.test import TestCase

from prospects.services.in_market_status import in_market_status
from prospects.tests.factories import make_prospect


class InMarketStatusTests(TestCase):
    def test_zero_intent_gives_no_signal(self):
        prospect = make_prospect()
        prospect.intent_score = 0
        result = in_market_status(prospect)
        self.assertEqual(result["code"], "no_signal")

    def test_high_intent_gives_strong_status(self):
        prospect = make_prospect()
        prospect.intent_score = 88
        result = in_market_status(prospect)
        self.assertEqual(result["code"], "strong")

    def test_mid_intent_gives_probable_status(self):
        prospect = make_prospect()
        prospect.intent_score = 65
        result = in_market_status(prospect)
        self.assertEqual(result["code"], "probable")

    def test_phrase_is_never_an_absolute_claim(self):
        prospect = make_prospect()
        for score in (0, 25, 50, 70, 95):
            prospect.intent_score = score
            phrase = in_market_status(prospect)["phrase"]
            self.assertNotIn("veut acheter", phrase.lower())
            self.assertNotIn("va acheter", phrase.lower())
