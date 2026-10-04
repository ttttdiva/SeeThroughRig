"""Optional production protocol; existing strig CLI/editor remain independent.

python -m seethrough_rig.integration COMMAND --json
"""
from __future__ import annotations
import argparse
import contextlib
import json
import sys
from pathlib import Path
from .production_io import ProductionError
from .package_validate import ValidationError

IDENTITY = 'seethrough-rig'
PROTOCOL_VERSION = 1
VERSION = '0.1.0'

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProductionError('invalid_arguments', message)

def parser():
    ap = Parser(description='Optional isolated See-through Rig production/export protocol v1')
    ap.add_argument('--json', action='store_true', help='structured protocol envelope (logs on stderr)')
    sub = ap.add_subparsers(dest='command', required=True, parser_class=Parser)
    def command(name, help):
        p = sub.add_parser(name, help=help)
        p.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        return p
    command('doctor', 'read local dependencies/inference/material readiness without loading models')
    for name in ('prepare', 'install', 'update'):
        command(name, 'explicit host operation; explicit lightweight preparation / pinned-release failclosed management')
    p = command('project-create', 'copy source into a durable isolated UUID project')
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--project-id', required=True)
    p.add_argument('--owner', required=True)
    p.add_argument('--character', required=True)
    p.add_argument('--projects-root', type=Path, help='host-controlled storage root')
    for name in ('project-status', 'project-run', 'project-resume', 'project-cancel', 'project-export'):
        p = command(name, {'project-status': 'inspect stage, deficits and immutable revision',
                           'project-run': 'run available decompose/build/validate/export stages',
                           'project-resume': 'clear cancellation and reconcile source/hash cache before retry',
                           'project-cancel': 'request cooperative cancellation without deleting inputs',
                           'project-export': 'export current matching build/adopted snapshot'}[name])
        p.add_argument('--project', required=True)
        p.add_argument('--projects-root', type=Path)
    for name in ('project-editor-info', 'project-editor-save'):
        p = command(name, 'read scoped editor assets or save validated project tuning for a new revision')
        p.add_argument('--project', required=True)
        p.add_argument('--projects-root', type=Path)
        if name == 'project-editor-save':
            p.add_argument('--settings-file', required=True, type=Path)
    p = command('project-adopt', 'explicitly approved existing model reuse; never claims new inference')
    p.add_argument('--project', required=True)
    p.add_argument('--projects-root', type=Path)
    p.add_argument('--model-dir', type=Path, required=True)
    p.add_argument('--source-commit', required=True)
    p.add_argument('--approved', action='store_true')
    p.add_argument('--preview', type=Path)
    p.add_argument('--settings', type=Path)
    p = command('export', 'strict immutable data ZIP with actual model preview and preserved settings')
    for name in ('model-dir', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    for name in ('project-id', 'revision', 'source-sha256', 'source-commit'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--profile', choices=('comparison-v1', 'conversation-v1'), default='comparison-v1')
    p.add_argument('--preview', type=Path)
    p.add_argument('--settings', type=Path)
    p.add_argument('--name')
    p.add_argument('--wind', type=float, help='conversation-v1: breeze 0..1')
    p.add_argument('--wind-blow', type=float, help='conversation-v1: wind blowing hair/ribbon/clothes 0..1')
    p = command('validate', 'validate archive bytes without extraction; recompute supported capabilities')
    p.add_argument('--package', type=Path, required=True)
    return ap

def dispatch(args):
    if args.command == 'doctor':
        from .production_environment import doctor
        return doctor()
    if args.command in ('prepare', 'install', 'update'):
        from .production_environment import prepare_environment
        return prepare_environment(operation=args.command)
    if args.command == 'export':
        from .production_export import export_package
        return export_package(args.model_dir, args.output, args.project_id, args.revision,
                              args.source_sha256, args.source_commit, args.profile, args.preview, args.settings, args.name,
                              wind=args.wind, wind_blow=args.wind_blow)
    if args.command == 'validate':
        from .package_validate import validate_package
        return validate_package(args.package)
    if args.command in ('project-editor-info', 'project-editor-save'):
        from .production_editor import editor_info, editor_save
        if args.command == 'project-editor-info':
            return editor_info(args.project, args.projects_root)
        return editor_save(args.project, args.settings_file, args.projects_root)
    from .production_projects import create_project, status, run_project, cancel, adopt_project
    if args.command == 'project-create':
        return create_project(args.source, args.project_id, args.owner, args.character, args.projects_root)
    if args.command == 'project-status':
        return status(args.project, args.projects_root)
    if args.command == 'project-cancel':
        return cancel(args.project, args.projects_root)
    if args.command == 'project-adopt':
        return adopt_project(args.project, args.model_dir, args.source_commit, args.approved, args.preview, args.settings, args.projects_root)
    return run_project(args.project, args.projects_root, resume=args.command == 'project-resume', export_only=args.command == 'project-export')

def main(argv=None):
    envelope = {'identity': IDENTITY, 'protocol_version': PROTOCOL_VERSION, 'ok': False, 'result': {}}
    code = 0
    try:
        args = parser().parse_args(argv)
        with contextlib.redirect_stdout(sys.stderr):
            envelope['result'] = dispatch(args)
        envelope['ok'] = True
    except ProductionError as exc:
        envelope['error'] = {'code': exc.code, 'message': str(exc)}
        code = 2 if exc.code == 'invalid_arguments' else 1
    except ValidationError as exc:
        envelope['error'] = {'code': exc.code, 'message': str(exc)}
        code = 1
    except KeyboardInterrupt:
        envelope['error'] = {'code': 'interrupted', 'message': 'Operation interrupted; project-resume safely reconciles durable state'}
        code = 130
    except Exception as exc:
        envelope['error'] = {'code': 'operation_failed', 'message': str(exc)}
        code = 1
    print(json.dumps(envelope, ensure_ascii=False, allow_nan=False), flush=True)
    return code

if __name__ == '__main__':
    raise SystemExit(main())
