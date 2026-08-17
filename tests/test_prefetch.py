"""The prefetch's exit code, which is the only guard the demo build has.

`RUN python -m pipelineguard.prefetch` is a build step, so a non-zero exit is
what stops a broken cache from being baked into an image. These tests fake the
cache state and assert the code, because the real failure -- a cache that looks
complete and still cannot resolve offline -- shipped once (findings §25.6).
"""
from __future__ import annotations

import pytest

from pipelineguard import prefetch
from pipelineguard.config import settings
from pipelineguard.detectors import tier2_encoder


@pytest.fixture
def fake_cache(monkeypatch):
    """A prefetch that downloads nothing, over a cache the test describes."""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(tier2_encoder, "prefetch_pinned",
                        lambda *a, **k: None)

    def describe(*, snapshots, main_ref):
        monkeypatch.setattr(tier2_encoder, "cached_base_revisions",
                            lambda *a, **k: snapshots)
        monkeypatch.setattr(tier2_encoder, "cached_main_revision",
                            lambda *a, **k: main_ref)

    return describe


def test_prefetch_fails_when_main_is_not_recorded(fake_cache):
    """The pinned commit is cached and nothing else is, yet a no-revision load
    resolves `main` and finds no ref. Looking complete is not being usable."""
    fake_cache(snapshots=[settings.tier2_base_revision], main_ref=None)
    assert prefetch.main([]) == 1


def test_prefetch_fails_when_main_points_somewhere_else(fake_cache):
    """An older cache can carry a ref to a different commit, which would load
    silently and quietly unpin the backbone."""
    fake_cache(snapshots=[settings.tier2_base_revision], main_ref="0" * 40)
    assert prefetch.main([]) == 1


def test_prefetch_succeeds_when_the_snapshot_and_the_ref_agree(fake_cache):
    fake_cache(snapshots=[settings.tier2_base_revision],
               main_ref=settings.tier2_base_revision)
    assert prefetch.main([]) == 0


def test_prefetch_still_warns_about_an_extra_cached_commit(fake_cache):
    """`main` resolving correctly does not prove the cache is clean -- another
    commit is still reachable by hash, so the exact-pin claim is not earned."""
    fake_cache(snapshots=["0" * 40, settings.tier2_base_revision],
               main_ref=settings.tier2_base_revision)
    assert prefetch.main([]) == 1


def test_go_offline_closes_the_network_for_an_already_imported_library(
        monkeypatch):
    """The environment variable on its own is not enough once huggingface_hub is
    imported: it reads the flag into a module constant at import time, and every
    network path asks the constant. A host with no build step downloads and then
    freezes inside one process, so the constant is the half that matters."""
    from huggingface_hub import constants

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    assert constants.is_offline_mode() is False

    prefetch.go_offline()

    assert constants.is_offline_mode() is True
    # The variable too, for child processes and for the UI's provenance panel,
    # which reports what the environment says.
    import os
    assert os.environ["HF_HUB_OFFLINE"] == "1"