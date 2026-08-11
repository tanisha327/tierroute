"""Table-driven tests for orchestrator.models.

Style note: each test enumerates its cases as a list of tuples and runs them under
subTest, so one failing case is reported precisely without masking the others. Tier
assertions compare against models.TIERS rather than hard-coded ids, so the suite
stays correct even when the tiers are overridden via GATEWAY_MODEL_* env vars.
"""

import unittest

from orchestrator import models


class ResolveTierTest(unittest.TestCase):
    def test_maps_to_concrete_ids(self):
        cases = [
            # (input tier, expected concrete id)
            ("cheap", models.TIERS["cheap"]),
            ("standard", models.TIERS["standard"]),
            ("premium", models.TIERS["premium"]),
            # loose aliases fold onto a canonical tier
            ("flash", models.TIERS["cheap"]),
            ("haiku", models.TIERS["cheap"]),
            ("frontier", models.TIERS["premium"]),
            ("reasoning", models.TIERS["premium"]),
            ("mid", models.TIERS["standard"]),
            # trimming + case-folding
            ("  Premium  ", models.TIERS["premium"]),
            ("CHEAP", models.TIERS["cheap"]),
            # unknown / empty -> standard fallback
            ("nonsense", models.TIERS["standard"]),
            ("", models.TIERS["standard"]),
            (None, models.TIERS["standard"]),
        ]
        for tier, expected in cases:
            with self.subTest(tier=tier):
                self.assertEqual(models.resolve_tier(tier), expected)


class ToCliTest(unittest.TestCase):
    def test_resolution_order(self):
        cases = [
            # canonical tier name -> tier id
            ("cheap", models.TIERS["cheap"]),
            ("premium", models.TIERS["premium"]),
            # loose tier alias -> tier id
            ("frontier", models.TIERS["premium"]),
            ("mid", models.TIERS["standard"]),
            # short model alias -> fixed concrete id (never env-dependent)
            ("haiku", "claude-haiku-4-5"),
            ("sonnet", "claude-sonnet-4-6"),
            ("opus", "claude-opus-4-8"),
            # already-concrete ids pass through untouched
            ("claude-opus-4-8", "claude-opus-4-8"),
            ("claude-sonnet-4-6-20260101", "claude-sonnet-4-6-20260101"),
            ("some-future-model", "some-future-model"),
            # whitespace trimmed; empty / None -> ""
            ("  opus  ", "claude-opus-4-8"),
            ("", ""),
            (None, ""),
        ]
        for model, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(models.to_cli(model), expected)


class RateForTest(unittest.TestCase):
    def test_longest_prefix_match(self):
        cases = [
            # exact known prefixes
            ("claude-opus-4", (15.0, 75.0)),
            ("claude-sonnet-4", (3.0, 15.0)),
            ("claude-haiku-4", (1.0, 5.0)),
            # dated suffixes still resolve via prefix match
            ("claude-opus-4-8-20260514", (15.0, 75.0)),
            ("claude-haiku-4-5", (1.0, 5.0)),
            # the 3.5 family is distinct from the 4 family
            ("claude-3-5-haiku-20241022", (0.80, 4.0)),
            ("claude-3-5-sonnet-20241022", (3.0, 15.0)),
            # unknown / empty / None -> default price
            ("gpt-4o", models._DEFAULT_PRICE),
            ("", models._DEFAULT_PRICE),
            (None, models._DEFAULT_PRICE),
        ]
        for model, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(models.rate_for(model), expected)


class CostTest(unittest.TestCase):
    def test_cost_math(self):
        # 1M input @ $15 + 1M output @ $75 = $90 for opus
        self.assertAlmostEqual(
            models.cost(
                "claude-opus-4", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
            ),
            90.0,
        )
        # haiku: 500k in @ $1/M + 200k out @ $5/M = 0.5 + 1.0 = 1.5
        self.assertAlmostEqual(
            models.cost(
                "claude-haiku-4", {"input_tokens": 500_000, "output_tokens": 200_000}
            ),
            1.5,
        )

    def test_missing_and_none_counts_are_zero(self):
        cases = [
            ("claude-opus-4", {}),
            ("claude-opus-4", None),
            ("claude-opus-4", {"input_tokens": None, "output_tokens": None}),
            ("claude-opus-4", {"input_tokens": 0, "output_tokens": 0}),
        ]
        for model, usage in cases:
            with self.subTest(usage=usage):
                self.assertEqual(models.cost(model, usage), 0.0)

    def test_unknown_model_uses_default_rate(self):
        # default (3, 15): 1M in + 1M out = 18
        self.assertAlmostEqual(
            models.cost(
                "mystery-model", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
            ),
            18.0,
        )


if __name__ == "__main__":
    unittest.main()
