"""Generate the import-syntax-portable runtime schema module from canonical JSON."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def generate():
    root = ROOT / 'web/js/runtime'
    schema = json.loads((root / 'playback-settings.schema.json').read_text(encoding='utf-8'))
    data = json.dumps(schema, ensure_ascii=False, indent=2)
    (root / 'playback-settings-schema.js').write_text('// Generated from playback-settings.schema.json. Regenerate with scripts/generate_playback_schema.py.\nexport default ' + data + ';\n', encoding='utf-8')
if __name__ == '__main__':
    generate()
