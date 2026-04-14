#!/usr/bin/env python3
"""
Unit and integration tests for delegate.py

Unit tests: mock LM Studio — verify routing logic, retry logic, escalation, model resolution.
Integration tests: require live LM Studio at localhost:1234 — marked with @live decorator.

Run unit tests only:
  python3 test_delegate.py unit

Run all (unit + live):
  python3 test_delegate.py all

Run specific class:
  python3 test_delegate.py TestRouting
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

# Add scripts/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import delegate

# ── Helpers ──────────────────────────────────────────────────────────────────

FULL_MODEL_LIST = [
    "qwen/qwen3-4b-2507",
    "qwen/qwen3-coder-next",
    "meta-llama-3.1-8b-instruct",
    "qwen/qwen3-30b-a3b-2507",
    "qwen/qwen3-4b-thinking-2507",
    "qwen3-0.6b-coders",
    "qwen3-0.6b",
    "qwen/qwen3-1.7b",
    "qwen/qwen3-coder-30b",
    "google/gemma-3-12b",
]

MINIMAL_MODEL_LIST = [
    "meta-llama-3.1-8b-instruct",
    "qwen/qwen3-4b-2507",
]

def live(fn):
    """Decorator: skip test if LM Studio is not reachable."""
    def wrapper(self, *args, **kwargs):
        if not delegate.check_health():
            self.skipTest("LM Studio not reachable at localhost:1234")
        return fn(self, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


# ── Unit Tests: Model Resolution ─────────────────────────────────────────────

class TestModelResolution(unittest.TestCase):

    def test_resolve_triage_primary(self):
        result = delegate.resolve_model("triage", FULL_MODEL_LIST)
        self.assertEqual(result, "qwen/qwen3-4b-2507")

    def test_resolve_triage_fallback(self):
        # Remove primary — should fall to 1.7b
        models = [m for m in FULL_MODEL_LIST if m != "qwen/qwen3-4b-2507"]
        result = delegate.resolve_model("triage", models)
        self.assertEqual(result, "qwen/qwen3-1.7b")

    def test_resolve_code_primary(self):
        result = delegate.resolve_model("code", FULL_MODEL_LIST)
        self.assertEqual(result, "qwen/qwen3-coder-next")

    def test_resolve_code_fallback(self):
        models = [m for m in FULL_MODEL_LIST if m != "qwen/qwen3-coder-next"]
        result = delegate.resolve_model("code", models)
        self.assertEqual(result, "qwen/qwen3-coder-30b")

    def test_resolve_fast_code_primary(self):
        result = delegate.resolve_model("fast_code", FULL_MODEL_LIST)
        self.assertEqual(result, "meta-llama-3.1-8b-instruct")

    def test_resolve_reasoning_primary(self):
        result = delegate.resolve_model("reasoning", FULL_MODEL_LIST)
        self.assertEqual(result, "qwen/qwen3-30b-a3b-2507")

    def test_resolve_unknown_role_returns_none(self):
        result = delegate.resolve_model("nonexistent_role", FULL_MODEL_LIST)
        self.assertIsNone(result)

    def test_resolve_no_candidates_available(self):
        result = delegate.resolve_model("code", ["meta-llama-3.1-8b-instruct"])
        self.assertIsNone(result)

    def test_all_roles_resolve_on_full_list(self):
        for role in delegate.ROUTING_TABLE:
            with self.subTest(role=role):
                result = delegate.resolve_model(role, FULL_MODEL_LIST)
                self.assertIsNotNone(result, f"Role '{role}' failed to resolve on full model list")


# ── Unit Tests: Complexity → Role Mapping ────────────────────────────────────

class TestRouting(unittest.TestCase):

    def test_complexity_1_no_type(self):
        self.assertEqual(delegate.determine_role(1, None), "classify")

    def test_complexity_2_no_type(self):
        self.assertEqual(delegate.determine_role(2, None), "fast_code")

    def test_complexity_3_code(self):
        self.assertEqual(delegate.determine_role(3, "code"), "fast_code")

    def test_complexity_3_reasoning(self):
        self.assertEqual(delegate.determine_role(3, "reasoning"), "fast_reasoning")

    def test_complexity_4_code(self):
        self.assertEqual(delegate.determine_role(4, "code"), "code")

    def test_complexity_4_reasoning(self):
        self.assertEqual(delegate.determine_role(4, "reasoning"), "reasoning")

    def test_complexity_5_code(self):
        self.assertEqual(delegate.determine_role(5, "code"), "code")

    def test_complexity_5_reasoning(self):
        self.assertEqual(delegate.determine_role(5, "reasoning"), "reasoning")

    def test_classify_type_always_classify_role(self):
        for c in [1, 2, 3, 4, 5]:
            with self.subTest(complexity=c):
                self.assertEqual(delegate.determine_role(c, "classify"), "classify")

    def test_out_of_range_complexity_falls_back(self):
        # complexity 0 and 9 not in map — should return "general"
        self.assertEqual(delegate.determine_role(0, None), "general")
        self.assertEqual(delegate.determine_role(9, None), "general")


# ── Unit Tests: Retry and Escalation ─────────────────────────────────────────

class TestRetryEscalation(unittest.TestCase):

    def _task(self, prompt="do something", task_type="code", max_retries=2, complexity=None):
        t = {"prompt": prompt, "type": task_type, "max_retries": max_retries}
        if complexity is not None:
            t["complexity"] = complexity
        return t

    @patch("delegate.call_model", return_value="def hello(): pass")
    @patch("delegate.analyse_complexity", return_value=2)
    def test_success_on_first_attempt(self, mock_complexity, mock_call):
        result = delegate.run_with_retry(self._task(complexity=2), FULL_MODEL_LIST)
        self.assertTrue(result["success"])
        self.assertFalse(result["escalate"])
        self.assertEqual(result["attempts"], 1)
        self.assertIn("hello", result["response"])

    @patch("delegate.call_model", side_effect=[Exception("timeout"), "def hello(): pass"])
    @patch("delegate.analyse_complexity", return_value=2)
    def test_success_on_second_attempt(self, mock_complexity, mock_call):
        result = delegate.run_with_retry(self._task(complexity=2), FULL_MODEL_LIST)
        self.assertTrue(result["success"])
        self.assertEqual(result["attempts"], 2)

    @patch("delegate.call_model", side_effect=Exception("timeout"))
    @patch("delegate.analyse_complexity", return_value=2)
    def test_escalates_after_all_retries(self, mock_complexity, mock_call):
        result = delegate.run_with_retry(self._task(complexity=2, max_retries=2), FULL_MODEL_LIST)
        self.assertFalse(result["success"])
        self.assertTrue(result["escalate"])
        self.assertEqual(result["attempts"], 3)  # max_retries + 1

    @patch("delegate.call_model")
    def test_escalates_immediately_when_no_model_available(self, mock_call):
        result = delegate.run_with_retry(
            self._task(complexity=4, task_type="code"),
            ["meta-llama-3.1-8b-instruct"]  # no code-role model in this list
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["escalate"])
        self.assertEqual(result["attempts"], 0)
        mock_call.assert_not_called()

    @patch("delegate.call_model", return_value="result")
    def test_complexity_override_skips_triage(self, mock_call):
        with patch("delegate.analyse_complexity") as mock_analyse:
            result = delegate.run_with_retry(
                self._task(complexity=3),  # explicit override
                FULL_MODEL_LIST
            )
            mock_analyse.assert_not_called()
        self.assertTrue(result["success"])

    @patch("delegate.call_model", side_effect=Exception("err"))
    def test_escalation_result_contains_reason(self, mock_call):
        result = delegate.run_with_retry(self._task(complexity=2, max_retries=1), FULL_MODEL_LIST)
        self.assertIn("reason", result)
        self.assertIn("attempts", result)
        self.assertIn("model", result)
        self.assertIn("role", result)


# ── Unit Tests: Health Check ──────────────────────────────────────────────────

class TestHealthCheck(unittest.TestCase):

    @patch("delegate._http", return_value={"data": []})
    def test_health_returns_true_when_reachable(self, mock_http):
        self.assertTrue(delegate.check_health())

    @patch("delegate._http", side_effect=ConnectionError("refused"))
    def test_health_returns_false_when_unreachable(self, mock_http):
        self.assertFalse(delegate.check_health())


# ── Unit Tests: fetch_models filters correctly ────────────────────────────────

class TestFetchModels(unittest.TestCase):

    @patch("delegate._http", return_value={"data": [
        {"id": "qwen/qwen3-coder-next", "object": "model"},
        {"id": "text-embedding-nomic-embed-text-v1.5", "object": "model"},
        {"id": "mlx-community/snowflake-arctic-embed-l-v2.0", "object": "model"},
        {"id": "meta-llama-3.1-8b-instruct", "object": "model"},
    ]})
    def test_filters_out_embedding_and_mlx_models(self, mock_http):
        with patch("builtins.open", unittest.mock.mock_open()):
            with patch("os.makedirs"):
                models = delegate.fetch_models()
        self.assertIn("qwen/qwen3-coder-next", models)
        self.assertIn("meta-llama-3.1-8b-instruct", models)
        self.assertNotIn("text-embedding-nomic-embed-text-v1.5", models)
        self.assertNotIn("mlx-community/snowflake-arctic-embed-l-v2.0", models)


# ── Unit Tests: Analyse Complexity Fallback ───────────────────────────────────

class TestAnalyseComplexity(unittest.TestCase):

    @patch("delegate.resolve_model", return_value=None)
    def test_returns_3_when_no_triage_model(self, mock_resolve):
        result = delegate.analyse_complexity("any prompt", FULL_MODEL_LIST)
        self.assertEqual(result, 3)

    @patch("delegate._http", return_value={
        "choices": [{"message": {"content": "4"}}]
    })
    @patch("delegate.resolve_model", return_value="qwen/qwen3-4b-2507")
    def test_returns_score_from_model(self, mock_resolve, mock_http):
        result = delegate.analyse_complexity("complex prompt", FULL_MODEL_LIST)
        self.assertEqual(result, 4)

    @patch("delegate._http", return_value={
        "choices": [{"message": {"content": "banana"}}]
    })
    @patch("delegate.resolve_model", return_value="qwen/qwen3-4b-2507")
    def test_returns_3_on_non_digit_response(self, mock_resolve, mock_http):
        result = delegate.analyse_complexity("prompt", FULL_MODEL_LIST)
        self.assertEqual(result, 3)

    @patch("delegate._http", side_effect=Exception("timeout"))
    @patch("delegate.resolve_model", return_value="qwen/qwen3-4b-2507")
    def test_returns_3_on_call_exception(self, mock_resolve, mock_http):
        result = delegate.analyse_complexity("prompt", FULL_MODEL_LIST)
        self.assertEqual(result, 3)


# ── Live Integration Tests ────────────────────────────────────────────────────

class TestLiveRouting(unittest.TestCase):
    """Requires LM Studio running at localhost:1234"""

    @live
    def test_health_check_passes(self):
        self.assertTrue(delegate.check_health())

    @live
    def test_fetch_models_returns_list(self):
        models = delegate.fetch_models()
        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)
        # All returned IDs should be non-embedding models
        for m in models:
            self.assertNotIn("embedding", m.lower())
            self.assertNotIn("mlx-community", m.lower())

    @live
    def test_triage_scores_simple_prompt(self):
        models = delegate.fetch_models()
        score = delegate.analyse_complexity("rename variable x to count", models)
        self.assertIn(score, [1, 2, 3])

    @live
    def test_triage_scores_complex_prompt(self):
        models = delegate.fetch_models()
        score = delegate.analyse_complexity(
            "Redesign the authentication system to support multi-tenant OAuth2 "
            "with dynamic client registration, zero downtime migration, and backward "
            "compatibility with existing JWT tokens across 15 microservices",
            models
        )
        self.assertGreaterEqual(score, 3)

    @live
    def test_route_simple_code_task(self):
        models = delegate.fetch_models()
        complexity = delegate.analyse_complexity("add a docstring to this function", models)
        role = delegate.determine_role(complexity, "code")
        model = delegate.resolve_model(role, models)
        self.assertIsNotNone(model)
        self.assertIn(role, ["fast_code", "classify"])

    @live
    def test_route_complex_reasoning_task(self):
        models = delegate.fetch_models()
        task = {
            "prompt": "Design a distributed rate limiter that works across 50 nodes "
                      "with sub-millisecond coordination and graceful degradation",
            "type": "reasoning",
            "max_retries": 1
        }
        result = delegate.run_with_retry(task, models)
        # Should succeed OR escalate cleanly — never raise an exception
        self.assertIn("success", result)
        self.assertIn("escalate", result)
        self.assertIn("model", result)
        if result["success"]:
            self.assertIsNotNone(result["response"])
            self.assertGreater(len(result["response"]), 10)

    @live
    def test_run_simple_task_end_to_end(self):
        models = delegate.fetch_models()
        task = {
            "prompt": "Write a Python function that returns the sum of a list of numbers.",
            "type": "code",
            "max_retries": 2
        }
        result = delegate.run_with_retry(task, models)
        self.assertIn("success", result)
        if result["success"]:
            self.assertIn("def ", result["response"])

    @live
    def test_escalation_path_on_no_model(self):
        """Force escalation by providing an empty model list."""
        task = {"prompt": "do something", "type": "code", "max_retries": 2, "complexity": 4}
        result = delegate.run_with_retry(task, [])
        self.assertFalse(result["success"])
        self.assertTrue(result["escalate"])
        self.assertEqual(result["attempts"], 0)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "unit"

    if mode == "unit":
        # Run only non-live test classes
        suite = unittest.TestSuite()
        for cls in [
            TestModelResolution, TestRouting, TestRetryEscalation,
            TestHealthCheck, TestFetchModels, TestAnalyseComplexity
        ]:
            suite.addTests(unittest.TestLoader().loadTestsFromTestCase(cls))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    elif mode == "live" or mode == "all":
        suite = unittest.TestSuite()
        unit_classes = [
            TestModelResolution, TestRouting, TestRetryEscalation,
            TestHealthCheck, TestFetchModels, TestAnalyseComplexity
        ]
        live_classes = [TestLiveRouting]
        for cls in (unit_classes + live_classes if mode == "all" else live_classes):
            suite.addTests(unittest.TestLoader().loadTestsFromTestCase(cls))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    else:
        # Treat as class name
        suite = unittest.TestLoader().loadTestsFromName(mode, sys.modules[__name__])
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
