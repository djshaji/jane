"""Camera CLI commands: no network access or inferred object identity."""

import json
from argparse import Namespace
from pathlib import Path

from jane.core.clock import SystemClock
from jane.core.contracts import ObservationRequest
from jane.storage.journal import Journal
from jane.storage.media import MediaStore
from jane.vision.camera import OpenCVCamera
from jane.vision.service import CameraService


def camera_command(args: Namespace) -> object:
    if args.operation == "capture" and (not args.enable_camera or not args.task):
        raise ValueError("capture requires --enable-camera and --task")
    if args.operation == "expire" and not args.asset:
        raise ValueError("expire requires --asset")
    camera = OpenCVCamera(args.device)
    journal = Journal(args.database)
    try:
        media = MediaStore(journal, Path(str(args.database) + ".media"))
        if args.operation == "capture":
            request = ObservationRequest(
                source_id=f"camera.{args.device}",
                modality="image",
                max_age_ms=10000,
                purpose="Operator requested capture",
            )
            event = CameraService(journal, media, camera, SystemClock()).capture(
                request, args.task, "turn-" + args.task, enabled=True, retain=args.retain_image
            )
            if not args.retain_image:
                media.expire(event.payload.asset_id)
            return {
                "retained": args.retain_image,
                "event": event.model_dump(mode="json"),
                "note": "No model inspected this image. Retention requires --retain-image.",
            }
        if args.operation == "expire":
            media.expire(args.asset)
            return {"expired": args.asset}
        if args.operation == "recover":
            return {"removed_orphans": media.recover()}
        return {
            "assets": [
                {"metadata": json.loads(row["body"]), "state": row["state"]}
                for row in journal.db.execute("SELECT * FROM assets ORDER BY asset_id")
            ]
        }
    finally:
        journal.close()
