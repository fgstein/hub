"""Écrit le schéma OpenAPI dans un fichier (génération des types TypeScript)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import Settings
from ..core.models import Inventory
from ..executors.fake import FakeExecutor
from ..service import InventoryManager, RunService, Store
from .app import create_app


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    svc = RunService(
        InventoryManager.from_inventory(Inventory()), FakeExecutor(), Store(":memory:")
    )
    app = create_app(Settings(), svc, enable_mcp=False)
    out.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n")
    print(f"OpenAPI écrit dans {out}")


if __name__ == "__main__":
    main()
