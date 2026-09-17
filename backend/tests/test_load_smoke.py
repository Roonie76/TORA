"""A small run of the load/robustness scenarios, so the release gate covers them.

The full run (hundreds of users, hundreds of fuzz payloads) lives in
backend/stress/load_test.py; this keeps the same checks at a size that fits CI.
"""
import argparse
import asyncio

import pytest

import backend.main as main_module
from backend.stress import load_test


@pytest.fixture
def restore_globals():
    saved = (main_module.agent.llm_provider, main_module.planner.llm_provider,
             main_module.auth_verifier, main_module.rate_limiter.limit)
    yield
    (main_module.agent.llm_provider, main_module.planner.llm_provider,
     main_module.auth_verifier, main_module.rate_limiter.limit) = saved
    main_module.rate_limiter.reset()


def test_load_scenarios_smoke(restore_globals):
    args = argparse.Namespace(users=8, isolation_users=4, parallel_turns=4, cancel=3, fuzz=20, json=None)
    report = asyncio.run(load_test.run(args))
    failures = {name: result for name, result in report.items()
                if isinstance(result, dict) and not result.get("ok")}
    assert not failures, failures
    assert report["throughput"]["turns"] == 24
    assert report["fuzz"]["server_error_count"] == 0
