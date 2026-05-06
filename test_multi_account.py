"""Tests for aws_cleaner/multi_account.py

Run:
    python3 test_multi_account.py
    python3 test_multi_account.py --filter aggregate
"""
import argparse
import sys
import unittest
from unittest.mock import MagicMock, patch

from aws_cleaner.config import ScanConfig
from aws_cleaner.multi_account import sweep_accounts, aggregate_sweep, render_sweep_summary


# ─── Fixtures ──────────────────────────────────────────────────────────────────

def _make_agent_result(service="s3", num_resources=3, num_recs=2, savings_per_rec=15.0):
    resources = [
        {"resource_id": f"bucket-{i}", "size_bytes": 1024 * i, "reason": "stale"}
        for i in range(num_resources)
    ]
    recs = [
        {"service": service, "resource_id": f"bucket-{i}", "monthly_savings_usd": savings_per_rec, "reason": "unused"}
        for i in range(num_recs)
    ]
    return {
        "discovered_resources": {service: resources},
        "recommendations": recs,
        "errors": [],
        "cost_data": {},
        "llm_analysis": "mock analysis",
    }


def _base_config():
    return ScanConfig(aws_profile="qa", aws_region="us-east-1", services=["s3"])


# ─── Tests ─────────────────────────────────────────────────────────────────────

class TestSweepAccounts(unittest.TestCase):
    """sweep_accounts runs one scan per profile and handles errors."""

    @patch("aws_cleaner.multi_account.run_agent")
    def test_returns_one_entry_per_profile(self, mock_run):
        mock_run.return_value = _make_agent_result()
        results = sweep_accounts(
            profiles=["qa", "staging", "prod"],
            base_config=_base_config(),
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(mock_run.call_count, 3)

    @patch("aws_cleaner.multi_account.run_agent")
    def test_profile_names_in_results(self, mock_run):
        mock_run.return_value = _make_agent_result()
        results = sweep_accounts(
            profiles=["qa", "prod"],
            base_config=_base_config(),
        )
        profile_names = {r["profile"] for r in results}
        self.assertEqual(profile_names, {"qa", "prod"})

    @patch("aws_cleaner.multi_account.run_agent")
    def test_failed_account_doesnt_abort_others(self, mock_run):
        def side_effect(config):
            if config.aws_profile == "broken":
                raise Exception("Auth failure")
            return _make_agent_result()

        mock_run.side_effect = side_effect
        results = sweep_accounts(
            profiles=["qa", "broken", "prod"],
            base_config=_base_config(),
        )
        self.assertEqual(len(results), 3)
        failed = next(r for r in results if r["profile"] == "broken")
        ok_qa = next(r for r in results if r["profile"] == "qa")
        self.assertIsNotNone(failed["error"])
        self.assertIsNone(ok_qa["error"])

    @patch("aws_cleaner.multi_account.run_agent")
    def test_config_overrides_profile_per_account(self, mock_run):
        mock_run.return_value = _make_agent_result()
        sweep_accounts(
            profiles=["alpha", "beta"],
            base_config=_base_config(),
        )
        called_profiles = {call.args[0].aws_profile for call in mock_run.call_args_list}
        self.assertEqual(called_profiles, {"alpha", "beta"})

    @patch("aws_cleaner.multi_account.run_agent")
    def test_region_override_applied(self, mock_run):
        mock_run.return_value = _make_agent_result()
        sweep_accounts(
            profiles=["qa"],
            base_config=_base_config(),
            region="eu-west-1",
        )
        self.assertEqual(mock_run.call_args[0][0].aws_region, "eu-west-1")

    @patch("aws_cleaner.multi_account.run_agent")
    def test_single_profile_works(self, mock_run):
        mock_run.return_value = _make_agent_result()
        results = sweep_accounts(profiles=["only-one"], base_config=_base_config())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["profile"], "only-one")
        self.assertIsNone(results[0]["error"])

    @patch("aws_cleaner.multi_account.run_agent")
    def test_results_sorted_by_profile(self, mock_run):
        mock_run.return_value = _make_agent_result()
        results = sweep_accounts(
            profiles=["zzz", "aaa", "mmm"],
            base_config=_base_config(),
        )
        names = [r["profile"] for r in results]
        self.assertEqual(names, sorted(names))


class TestAggregatesSweep(unittest.TestCase):
    """aggregate_sweep merges per-account data correctly."""

    def _make_sweep_results(self, profiles_data: dict):
        """Helper: {profile: (num_resources, num_recs, savings)} or {profile: 'error'}."""
        results = []
        for profile, data in profiles_data.items():
            if data == "error":
                results.append({"profile": profile, "region": "us-east-1", "result": None, "error": "Auth failed"})
            else:
                num_res, num_recs, savings = data
                results.append({
                    "profile": profile,
                    "region": "us-east-1",
                    "result": _make_agent_result(num_resources=num_res, num_recs=num_recs, savings_per_rec=savings),
                    "error": None,
                })
        return results

    def test_totals_sum_correctly(self):
        sweep = self._make_sweep_results({"qa": (3, 2, 10.0), "prod": (5, 3, 20.0)})
        agg = aggregate_sweep(sweep)
        self.assertEqual(agg["total_resources"], 8)
        self.assertAlmostEqual(agg["total_savings_usd"], 80.0)  # 2*10 + 3*20

    def test_failed_account_excluded_from_totals(self):
        sweep = self._make_sweep_results({"qa": (3, 2, 10.0), "broken": "error"})
        agg = aggregate_sweep(sweep)
        self.assertEqual(agg["total_resources"], 3)
        self.assertEqual(agg["accounts_failed"], 1)

    def test_accounts_scanned_count(self):
        sweep = self._make_sweep_results({"a": (1, 0, 0), "b": (0, 0, 0), "c": "error"})
        agg = aggregate_sweep(sweep)
        self.assertEqual(agg["accounts_scanned"], 3)

    def test_by_account_contains_all_profiles(self):
        sweep = self._make_sweep_results({"qa": (2, 1, 5.0), "prod": "error"})
        agg = aggregate_sweep(sweep)
        self.assertIn("qa", agg["by_account"])
        self.assertIn("prod", agg["by_account"])

    def test_all_recommendations_annotated_with_account(self):
        sweep = self._make_sweep_results({"qa": (2, 2, 5.0), "prod": (1, 1, 8.0)})
        agg = aggregate_sweep(sweep)
        for rec in agg["all_recommendations"]:
            self.assertIn("_account", rec)
            self.assertIn(rec["_account"], {"qa", "prod"})

    def test_by_service_aggregates_across_accounts(self):
        sweep = self._make_sweep_results({"qa": (3, 1, 5.0), "prod": (2, 1, 8.0)})
        agg = aggregate_sweep(sweep)
        # Both use "s3" service — should have 5 total resources
        self.assertEqual(len(agg["by_service"]["s3"]), 5)

    def test_empty_sweep_returns_zeros(self):
        agg = aggregate_sweep([])
        self.assertEqual(agg["total_resources"], 0)
        self.assertEqual(agg["total_savings_usd"], 0.0)
        self.assertEqual(agg["accounts_scanned"], 0)

    def test_all_failed_returns_zero_resources(self):
        sweep = self._make_sweep_results({"a": "error", "b": "error"})
        agg = aggregate_sweep(sweep)
        self.assertEqual(agg["total_resources"], 0)
        self.assertEqual(agg["accounts_failed"], 2)


class TestRenderSweepSummary(unittest.TestCase):
    """render_sweep_summary should not raise for any input."""

    @patch("aws_cleaner.multi_account.run_agent")
    def test_renders_without_error(self, mock_run):
        mock_run.return_value = _make_agent_result()
        sweep = sweep_accounts(profiles=["qa", "prod"], base_config=_base_config())
        render_sweep_summary(sweep)  # no exception

    def test_renders_all_failed(self):
        sweep = [
            {"profile": "qa", "region": "us-east-1", "result": None, "error": "No credentials"},
            {"profile": "prod", "region": "us-east-1", "result": None, "error": "Timeout"},
        ]
        render_sweep_summary(sweep)  # no exception

    def test_renders_empty(self):
        render_sweep_summary([])  # no exception


# ─── Runner ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-account sweep tests")
    parser.add_argument("--filter", help="Only run tests matching this string")
    args, _ = parser.parse_known_args()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestSweepAccounts,
        TestAggregatesSweep,
        TestRenderSweepSummary,
    ]

    for cls in test_classes:
        tests = loader.loadTestsFromTestCase(cls)
        if args.filter:
            tests = unittest.TestSuite(
                t for t in tests if args.filter.lower() in t._testMethodName.lower()
            )
        suite.addTests(tests)

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
