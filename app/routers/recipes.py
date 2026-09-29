import sqlalchemy as sa
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import api_error
from ..models import Recipe, RecipeVersion, utcnow
from ..schemas import (
    RecipeCreate,
    RecipeVersionCreate,
    RecipeVersionUpdate,
    normalize_code,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])


def serialize_version(rv: RecipeVersion) -> dict:
    return {
        "version": rv.version,
        "status": rv.status,
        "items": rv.items,
        "yield_quantity": rv.yield_quantity,
        "yield_unit": rv.yield_unit,
        "created_at": rv.created_at.isoformat() if rv.created_at else None,
        "published_at": rv.published_at.isoformat() if rv.published_at else None,
    }


def serialize_recipe(recipe: Recipe) -> dict:
    return {
        "code": recipe.code,
        "name": recipe.name,
        "created_at": recipe.created_at.isoformat() if recipe.created_at else None,
        "versions": [serialize_version(rv) for rv in recipe.versions],
    }


def _get_recipe_or_404(db: Session, code: str) -> Recipe:
    recipe = db.query(Recipe).filter(Recipe.code == normalize_code(code)).first()
    if recipe is None:
        raise api_error(404, "RECIPE_NOT_FOUND", f"unknown recipe: {code!r}")
    return recipe


def _get_version_or_404(db: Session, recipe: Recipe, version: int) -> RecipeVersion:
    rv = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.recipe_id == recipe.id, RecipeVersion.version == version)
        .first()
    )
    if rv is None:
        raise api_error(
            404, "RECIPE_VERSION_NOT_FOUND", f"recipe {recipe.code!r} has no version {version}"
        )
    return rv


def _fresh_status(db: Session, version_id) -> str | None:
    """Re-read the committed status of a version row, bypassing the identity map.

    Under READ COMMITTED this observes any concurrent transaction that committed
    after our initial read, which is exactly the state a state-guarded write
    just lost to. Returns None if the row no longer exists.
    """
    return db.execute(
        sa.select(RecipeVersion.status).where(RecipeVersion.id == version_id)
    ).scalar_one_or_none()


def _raise_lost_race(db: Session, recipe: Recipe, version: int, version_id) -> None:
    """Explain why a state-guarded write matched no row.

    The row we read as a draft was either published (409) or deleted (404) by
    a concurrent request that committed first. Status is a one-way latch
    (draft -> published) and ids are never reused, so a row that still exists
    at this point can only be published.
    """
    if _fresh_status(db, version_id) is None:
        raise api_error(
            404,
            "RECIPE_VERSION_NOT_FOUND",
            f"recipe {recipe.code!r} has no version {version}",
        )
    raise api_error(
        409,
        "RECIPE_VERSION_IMMUTABLE",
        f"recipe {recipe.code!r} version {version} is published and immutable",
    )


@router.post("", status_code=201)
def create_recipe(body: RecipeCreate, db: Session = Depends(get_db)):
    if db.query(Recipe.id).filter(Recipe.code == body.code).first():
        raise api_error(409, "RECIPE_EXISTS", f"recipe {body.code!r} already exists")
    recipe = Recipe(code=body.code, name=body.name)
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return serialize_recipe(recipe)


@router.get("")
def list_recipes(db: Session = Depends(get_db)):
    return [serialize_recipe(r) for r in db.query(Recipe).order_by(Recipe.code).all()]


@router.get("/{code}")
def get_recipe(code: str, db: Session = Depends(get_db)):
    return serialize_recipe(_get_recipe_or_404(db, code))


@router.post("/{code}/versions", status_code=201)
def create_version(code: str, body: RecipeVersionCreate, db: Session = Depends(get_db)):
    """Add a new draft version to a recipe. Version numbers cannot be reused."""
    recipe = _get_recipe_or_404(db, code)
    exists = (
        db.query(RecipeVersion.id)
        .filter(RecipeVersion.recipe_id == recipe.id, RecipeVersion.version == body.version)
        .first()
    )
    if exists:
        raise api_error(
            409,
            "RECIPE_VERSION_EXISTS",
            f"recipe {recipe.code!r} already has a version {body.version}",
        )
    rv = RecipeVersion(
        recipe_id=recipe.id,
        version=body.version,
        status="draft",
        items=[i.model_dump(mode="json") for i in body.items],
        yield_quantity=body.yield_quantity,
        yield_unit=body.yield_unit,
    )
    db.add(rv)
    db.commit()
    db.refresh(rv)
    return serialize_version(rv)


@router.get("/{code}/versions/{version}")
def get_version(code: str, version: int, db: Session = Depends(get_db)):
    recipe = _get_recipe_or_404(db, code)
    return serialize_version(_get_version_or_404(db, recipe, version))


@router.put("/{code}/versions/{version}")
def replace_version(
    code: str, version: int, body: RecipeVersionUpdate, db: Session = Depends(get_db)
):
    """Replace a draft version. Published versions are immutable."""
    recipe = _get_recipe_or_404(db, code)
    rv = _get_version_or_404(db, recipe, version)
    if rv.status == "published":
        raise api_error(
            409,
            "RECIPE_VERSION_IMMUTABLE",
            f"recipe {recipe.code!r} version {version} is published and immutable",
        )
    # Atomic compare-and-swap: the row is updated only if it is still a draft
    # at write time. A concurrent publish that commits first flips the status
    # and makes this statement match zero rows, so the published row and its
    # content stay untouched.
    result = db.execute(
        sa.update(RecipeVersion)
        .where(RecipeVersion.id == rv.id, RecipeVersion.status == "draft")
        .values(
            items=[i.model_dump(mode="json") for i in body.items],
            yield_quantity=body.yield_quantity,
            yield_unit=body.yield_unit,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        _raise_lost_race(db, recipe, version, rv.id)
    db.commit()
    db.refresh(rv)
    return serialize_version(rv)


@router.delete("/{code}/versions/{version}", status_code=204)
def delete_version(code: str, version: int, db: Session = Depends(get_db)):
    """Delete a draft version. Published versions are immutable."""
    recipe = _get_recipe_or_404(db, code)
    rv = _get_version_or_404(db, recipe, version)
    if rv.status == "published":
        raise api_error(
            409,
            "RECIPE_VERSION_IMMUTABLE",
            f"recipe {recipe.code!r} version {version} is published and immutable",
        )
    # Same compare-and-swap as replace: only a row that is still a draft at
    # write time is deleted; a published row can never disappear.
    result = db.execute(
        sa.delete(RecipeVersion)
        .where(RecipeVersion.id == rv.id, RecipeVersion.status == "draft")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        _raise_lost_race(db, recipe, version, rv.id)
    db.commit()
    return Response(status_code=204)


@router.post("/{code}/versions/{version}/publish")
def publish_version(code: str, version: int, db: Session = Depends(get_db)):
    """Publish a recipe version, freezing it forever. Idempotent."""
    recipe = _get_recipe_or_404(db, code)
    rv = _get_version_or_404(db, recipe, version)
    if rv.status != "published":
        # Atomic compare-and-swap: flip draft -> published only if the row is
        # still a draft at write time. If a draft write (PUT) committed first,
        # this freezes the updated content; if a DELETE committed first, the
        # row is gone and there is nothing to publish.
        result = db.execute(
            sa.update(RecipeVersion)
            .where(RecipeVersion.id == rv.id, RecipeVersion.status == "draft")
            .values(status="published", published_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            # Lost the race: a concurrent publish committed first (idempotent
            # success below) or the draft was deleted (nothing to publish).
            if _fresh_status(db, rv.id) is None:
                raise api_error(
                    404,
                    "RECIPE_VERSION_NOT_FOUND",
                    f"recipe {recipe.code!r} has no version {version}",
                )
        else:
            db.commit()
        db.refresh(rv)
    return serialize_version(rv)
