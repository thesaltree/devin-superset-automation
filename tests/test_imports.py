"""Smoke test that imports work."""
def test_import_app():
    from app import main, orchestrator, worker_pool, ci_feedback, devin_client, github_client
    assert main.app is not None
