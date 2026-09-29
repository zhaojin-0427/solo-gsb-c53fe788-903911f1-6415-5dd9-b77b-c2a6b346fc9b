"""Regression tests for draft-vs-publish races on recipe versions.

The race: a request reads a draft version, a concurrent request commits a
publish/delete first, and the stale request must not be able to rewrite or
remove the now-published row. The interleaving is reproduced deterministically
by committing the "concurrent" write on the request's own session right after
its read, before its state-guarded write executes.
"""

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.models import RecipeVersion
from app.routers import recipes as recipes_router
from tests.helpers import make_recipe

ITEMS_A = [{"kind": "ingredient", "code": "sugar", "quantity": 10, "unit": "g"}]
ITEMS_B = [{"kind": "ingredient", "code": "sugar", "quantity": 99, "unit": "g"}]


def stored(items):
    """Expected stored shape: pydantic adds version=None, quantities are floats."""
    return [{**i, "version": None, "quantity": float(i["quantity"])} for i in items]

# Fixed timestamp used by the simulated concurrent publish, so the test can
# prove the first publish wins and is never overwritten.
FIRST_PUBLISHED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def interleave(monkeypatch):
    """Run a write on the request session right after the version row is read.

    Returns a function taking a callable ``write(db, rv)``; the callable runs
    exactly once, between the endpoint's read of the draft and its own write.
    """

    def install(write):
        original = recipes_router._get_version_or_404
        fired = {"done": False}

        def wrapper(db, recipe, version):
            rv = original(db, recipe, version)
            if not fired["done"]:
                fired["done"] = True
                write(db, rv)
            return rv

        monkeypatch.setattr(recipes_router, "_get_version_or_404", wrapper)

    return install


def concurrent_publish(db, rv):
    # synchronize_session=False keeps the request's in-memory rv stale
    # (status="draft"), exactly like a publish committed by another session.
    db.execute(
        sa.update(RecipeVersion)
        .where(RecipeVersion.id == rv.id)
        .values(status="published", published_at=FIRST_PUBLISHED_AT)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def concurrent_delete(db, rv):
    db.execute(
        sa.delete(RecipeVersion)
        .where(RecipeVersion.id == rv.id)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def make_draft(client, code="r"):
    make_recipe(client, code, [{"version": 1, "items": ITEMS_A}], publish=False)


def test_put_loses_to_concurrent_publish(client, interleave):
    """Publish commits first: the stale PUT gets 409 and the published row is unchanged."""
    make_draft(client)
    interleave(concurrent_publish)

    resp = client.put("/recipes/r/versions/1", json={"items": ITEMS_B})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_IMMUTABLE"

    rv = client.get("/recipes/r/versions/1").json()
    assert rv["status"] == "published"
    assert rv["items"] == stored(ITEMS_A)
    assert rv["published_at"].startswith("2026-01-01T12:00:00")


def test_delete_loses_to_concurrent_publish(client, interleave):
    """Publish commits first: the stale DELETE gets 409 and the row survives."""
    make_draft(client)
    interleave(concurrent_publish)

    resp = client.delete("/recipes/r/versions/1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_IMMUTABLE"

    rv = client.get("/recipes/r/versions/1").json()
    assert rv["status"] == "published"
    assert rv["items"] == stored(ITEMS_A)


def test_put_loses_to_concurrent_delete(client, interleave):
    """Delete commits first: the stale PUT gets 404 and the version stays gone."""
    make_draft(client)
    interleave(concurrent_delete)

    resp = client.put("/recipes/r/versions/1", json={"items": ITEMS_B})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"

    assert client.get("/recipes/r/versions/1").status_code == 404


def test_publish_loses_to_concurrent_delete(client, interleave):
    """Delete commits first: publish must fail and leave no snapshot behind."""
    make_draft(client)
    interleave(concurrent_delete)

    resp = client.post("/recipes/r/versions/1/publish")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"

    assert client.get("/recipes/r/versions/1").status_code == 404
    assert client.get("/recipes/r").json()["versions"] == []


def test_concurrent_publish_is_idempotent(client, interleave):
    """Publish loses to another publish: idempotent 200, first publish wins."""
    make_draft(client)
    interleave(concurrent_publish)

    resp = client.post("/recipes/r/versions/1/publish")
    assert resp.status_code == 200
    rv = resp.json()
    assert rv["status"] == "published"
    assert rv["items"] == stored(ITEMS_A)
    # The first publish's timestamp is kept, not overwritten.
    assert rv["published_at"].startswith("2026-01-01T12:00:00")


def test_put_does_not_clobber_recreated_version(client, interleave):
    """Delete + recreate commits first: the stale PUT targets the old row id,
    gets 404, and the new draft of the same version number is untouched."""

    def delete_and_recreate(db, rv):
        db.execute(
            sa.delete(RecipeVersion)
            .where(RecipeVersion.id == rv.id)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        client.post("/recipes/r/versions", json={"version": 1, "items": ITEMS_B})

    make_draft(client)
    interleave(delete_and_recreate)

    resp = client.put(
        "/recipes/r/versions/1",
        json={"items": [{"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "g"}]},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"

    rv = client.get("/recipes/r/versions/1").json()
    assert rv["status"] == "draft"
    assert rv["items"] == stored(ITEMS_B)


def test_put_wins_then_publish_freezes_updated_draft(client):
    """PUT commits first: a later publish freezes the updated content."""
    make_draft(client)

    resp = client.put(
        "/recipes/r/versions/1",
        json={"items": ITEMS_B, "yield_quantity": 50, "yield_unit": "g"},
    )
    assert resp.status_code == 200

    resp = client.post("/recipes/r/versions/1/publish")
    assert resp.status_code == 200
    rv = resp.json()
    assert rv["status"] == "published"
    assert rv["items"] == stored(ITEMS_B)
    assert rv["yield_quantity"] == 50
    assert rv["yield_unit"] == "g"

    # The frozen content is what evaluations will see from now on.
    rv = client.get("/recipes/r/versions/1").json()
    assert rv["items"] == stored(ITEMS_B)
    assert rv["yield_quantity"] == 50


def test_delete_wins_then_recreate_and_publish(client):
    """Delete commits first, the version is recreated and published: the new
    row is an independent draft and publishes cleanly."""
    make_draft(client)
    assert client.delete("/recipes/r/versions/1").status_code == 204

    resp = client.post("/recipes/r/versions", json={"version": 1, "items": ITEMS_B})
    assert resp.status_code == 201
    resp = client.post("/recipes/r/versions/1/publish")
    assert resp.status_code == 200
    assert resp.json()["items"] == stored(ITEMS_B)
