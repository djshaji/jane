"""Atomic media finalization, integrity checks and explicit retention cleanup."""

import hashlib
import importlib
import io
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from jane.core.contracts import Contract, Identifier

if TYPE_CHECKING:
    from jane.storage.journal import Journal

MAX_IMAGE_BYTES = 4 * 1024 * 1024


class ImageAsset(Contract):
    asset_id: Identifier
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    size: Annotated[int, Field(gt=0, le=MAX_IMAGE_BYTES)]
    mime: Literal["image/png"] = "image/png"
    width: Annotated[int, Field(gt=0, le=4096)]
    height: Annotated[int, Field(gt=0, le=4096)]
    retain: bool = False
    created_at: AwareDatetime
    quality: Literal["usable", "uncertain"]


def normalize(data: bytes) -> tuple[bytes, int, int, str]:
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("empty or oversized image")
    try:
        image_module = importlib.import_module("PIL.Image")
        stats = importlib.import_module("PIL.ImageStat")
        with image_module.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG"} or getattr(image, "n_frames", 1) != 1:
                raise ValueError("single JPEG/PNG required")
            width, height = image.size
            if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 4_000_000:
                raise ValueError("image dimensions exceed limits")
            image.load()
            rgb = image.convert("RGB")
            rgb.info.clear()
            # Low contrast is only a quality hint, never an object/occlusion claim.
            deviation = stats.Stat(rgb.convert("L")).stddev[0]
            quality = "uncertain" if deviation < 10 else "usable"
            output = io.BytesIO()
            rgb.save(output, format="PNG")  # Strip EXIF, location and other supplied metadata.
            encoded = output.getvalue()
            if len(encoded) > MAX_IMAGE_BYTES:
                raise ValueError("normalized image exceeds limit")
            return encoded, width, height, quality
    except (ImportError, OSError, ValueError):
        raise ValueError("invalid image or missing camera extra (Pillow)") from None


def read_asset(journal: "Journal", asset_id: str) -> tuple[ImageAsset, bytes]:
    row = journal.db.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None or row["state"] != "retained":
        raise ValueError("image is missing or expired; capture again")
    asset = ImageAsset.model_validate_json(row["body"])
    path = Path(row["path"])
    if path.is_symlink() or not path.is_file() or path.stat().st_size != asset.size:
        raise ValueError("image is unavailable; capture again")
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.digest:
        raise ValueError("image integrity check failed; capture again")
    return asset, data


class MediaStore:
    def __init__(self, journal: "Journal", root: Path) -> None:
        self.journal = journal
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def stage(self, data: bytes, at: object, *, retain: bool = False) -> ImageAsset:
        encoded, width, height, quality = normalize(data)
        asset = ImageAsset.model_validate(
            {
                "asset_id": "image-" + uuid4().hex,
                "digest": hashlib.sha256(encoded).hexdigest(),
                "size": len(encoded),
                "width": width,
                "height": height,
                "created_at": at,
                "retain": retain,
                "quality": quality,
            }
        )
        final = self.root / (asset.asset_id + ".png")
        temporary = self.root / (asset.asset_id + ".tmp")
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, final)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            # Caller groups this row and its event in the same journal transaction.
            self.journal.db.execute(
                "INSERT INTO assets VALUES(?,?,?,'retained')",
                (asset.asset_id, str(final), asset.model_dump_json()),
            )
            return asset
        finally:
            temporary.unlink(missing_ok=True)

    def expire(self, asset_id: str) -> None:
        row = self.journal.db.execute(
            "SELECT path FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown asset")
        path = Path(row[0])
        if path.parent != self.root:
            raise ValueError("asset belongs to another media directory")
        # Mark first: a crash cannot leave deleted media advertised as retained.
        with self.journal.transaction():
            self.journal.db.execute(
                "UPDATE assets SET state='expired' WHERE asset_id=?", (asset_id,)
            )
        path.unlink(missing_ok=True)

    def recover(self) -> int:
        """Exclusive startup only; remove uncommitted files and finish interrupted expiry."""
        for row in self.journal.db.execute(
            "SELECT body,path FROM assets WHERE state='retained'"
        ).fetchall():
            asset = ImageAsset.model_validate_json(row[0])
            if not asset.retain and Path(row[1]).parent == self.root:
                self.expire(asset.asset_id)
        retained = {
            Path(r[0])
            for r in self.journal.db.execute("SELECT path FROM assets WHERE state='retained'")
        }
        removed = 0
        for path in self.root.iterdir():
            if (
                path.name.startswith("image-")
                and path.suffix in {".tmp", ".png"}
                and path not in retained
            ):
                path.unlink()
                removed += 1
        return removed
