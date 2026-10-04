"""Data-only playback contract, read from the canonical runtime's schema.

This module does not implement animation. Python and JS validate the same keys,
bounds, defaults and cross-field constraints from one versioned JSON definition.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / 'web/js/runtime/playback-settings.schema.json'
DEFINITION = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
VERSION = DEFINITION['version']
GROUPS = {group['id']: {field['id']: field for field in group['fields']} for group in DEFINITION['groups']}
DEFAULTS = {'version': VERSION, **{group: {key: field['default'] for key, field in fields.items()} for group, fields in GROUPS.items()}}


def _require(condition, message):
    # Lazy import avoids the validator/model schema dependency cycle.
    from .package_validate import require
    require(condition, 'Invalid playbackSettings: ' + message)


def _constraints(settings):
    def at(path):
        group, key = path.split('.')
        return settings.get(group, {}).get(key)
    for rule in DEFINITION.get('constraints', []):
        # A partial layer may inherit its condition (e.g. fixed blink mode) from
        # an earlier layer. Defer conditional checks until the condition exists.
        active = all(at(path) is not None and at(path) == expected
                     for path, expected in rule.get('when', {}).items())
        if active and all(at(path) is not None for path in rule['paths']):
            _require(rule['kind'] == 'ordered' and at(rule['paths'][0]) <= at(rule['paths'][1]),
                     ' <= '.join(rule['paths']))


def validate_playback_settings(value, *, complete=False):
    """Validate a persisted v1 block and return a detached partial/full copy."""
    _require(isinstance(value, dict), 'must be an object')
    _require(type(value.get('version')) is int and value['version'] == VERSION, 'unsupported version')
    _require(set(value) <= {'version', *GROUPS}, 'unknown group')
    if complete:
        _require(set(value) == {'version', *GROUPS}, 'missing group')
    result = {'version': VERSION}
    for group, values in value.items():
        if group == 'version':
            continue
        fields = GROUPS[group]
        _require(isinstance(values, dict) and set(values) <= set(fields), 'unknown field in ' + group)
        if complete:
            _require(set(values) == set(fields), 'missing field in ' + group)
        result[group] = {}
        for key, item in values.items():
            field = fields[key]
            if field['type'] == 'boolean':
                valid = type(item) is bool
            elif field['type'] == 'number':
                valid = type(item) in (int, float) and math.isfinite(item) and field['min'] <= item <= field['max']
            else:
                valid = isinstance(item, str) and item in {option['value'] for option in field['options']}
            _require(valid, 'invalid ' + group + '.' + key)
            result[group][key] = item
    _constraints(result)
    return result


def resolve_playback_settings(*layers):
    result = copy.deepcopy(DEFAULTS)
    for layer in layers:
        if layer is None:
            continue
        for group, values in validate_playback_settings(layer).items():
            if group != 'version':
                result[group].update(values)
    _constraints(result)
    return result


def profile_playback_settings(profile):
    """Map legacy profile fields, leaving the motion implementation in JS."""
    profile = profile or {}
    return {'version': VERSION,
            'wind': {key: profile.get(key, 0) for key in ('wind', 'windBlow')},
            'idle': {'enabled': profile.get('idle', True)},
            'breath': {'enabled': profile.get('breath', True)},
            'blink': {'enabled': profile.get('blink', True)},
            'gaze': {'enabled': profile.get('saccade', True), 'tracking': profile.get('lookAt', False),
                     'follow': profile.get('follow', 0.6)}}


def package_playback_settings(model, settings=None, profile=None, *, wind=None, wind_blow=None):
    """Produce the immutable full manifest defaults, including explicit CLI wind."""
    overrides = {'version': VERSION, 'wind': {}}
    if wind is not None:
        overrides['wind']['wind'] = wind
    if wind_blow is not None:
        overrides['wind']['windBlow'] = wind_blow
    return resolve_playback_settings(profile_playback_settings(profile), model.get('playbackSettings'),
                                     (settings or {}).get('playbackSettings'), overrides)
