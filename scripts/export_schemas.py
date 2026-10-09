"""Export shared executable contracts; --check rejects stale checked-in artifacts."""

import argparse
import json
import tempfile
from pathlib import Path

from tts_api_server.fake import FakeAdapter
from tts_api_server.http import create_app
from tts_api_server.models import REQUESTS, VoiceRegistration
from tts_api_server.service import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "schemas"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        app = create_app([FakeAdapter()], Settings(data_dir=Path(directory)))
        try:
            artifacts = {
                "openapi.json": app.openapi(),
                "requests.json": {
                    name: model.model_json_schema()
                    for name, model in dict(REQUESTS, voice_registration=VoiceRegistration).items()
                },
            }
        finally:
            app.state.service.close()
    for filename, data in artifacts.items():
        value = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        path = root / filename
        if args.check:
            if not path.exists() or path.read_text() != value:
                raise SystemExit(f"Stale schema: {filename}; run scripts/export_schemas.py")
        else:
            path.write_text(value)


if __name__ == "__main__":
    main()
