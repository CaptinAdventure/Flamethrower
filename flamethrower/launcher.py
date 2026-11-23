import argparse
import json
import os
import secrets
import signal
import sys
import tempfile
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from wsgiref.simple_server import make_server

import portalocker


def read_runtime_record(path):
    if path.is_symlink():
        raise ValueError(f"Refusing symlinked runtime file: {path}")
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError as error:
        raise ValueError(f"Invalid runtime file; existing file was not changed: {path}") from error
    try:
        record = json.loads(contents)
    except ValueError as error:
        raise ValueError(f"Invalid runtime file; existing file was not changed: {path}") from error
    if (
        not isinstance(record, dict)
        or set(record) not in ({"application", "pid", "port"}, {"application", "pid", "port", "token"})
        or record["application"] != "flamethrower"
        or type(record["pid"]) is not int or record["pid"] <= 0
        or type(record["port"]) is not int or not 1 <= record["port"] <= 65535
        or ("token" in record and (
            not isinstance(record["token"], str) or not record["token"].isascii() or len(record["token"]) < 32
        ))
    ):
        raise ValueError(f"Invalid runtime file; existing file was not changed: {path}")
    return record


@contextmanager
def runtime_record(storage, port, token=None):
    path = storage / "server.json"
    record = {"application": "flamethrower", "pid": os.getpid(), "port": port}
    if token is not None:
        record["token"] = token
    read_runtime_record(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=storage, prefix=".server-", delete=False) as file:
            temporary = Path(file.name)
            json.dump(record, file)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    try:
        yield
    finally:
        try:
            if read_runtime_record(path) == record:
                path.unlink(missing_ok=True)
        except (OSError, ValueError) as error:
            print(f"Could not remove runtime file {path}: {error}", file=sys.stderr)


@contextmanager
def handle_termination():
    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def prepare_storage(project):
    storage = project / ".flamethrower"
    if storage.is_symlink():
        raise ValueError(f"Refusing symlinked storage directory: {storage}")
    storage.mkdir(mode=0o700, exist_ok=True)
    ignore = storage / ".gitignore"
    if ignore.is_symlink():
        raise ValueError(f"Refusing symlinked ignore file: {ignore}")
    if not ignore.exists():
        try:
            with ignore.open("x", encoding="utf-8") as file:
                file.write("*\n")
        except FileExistsError:
            pass
    if ignore.is_symlink() or ignore.read_text(encoding="utf-8") != "*\n":
        raise ValueError(f"Expected {ignore} to contain only '*'; existing file was not changed.")
    if (storage / "tasks.sqlite3").is_symlink():
        raise ValueError("Refusing symlinked task database.")


def main():
    parser = argparse.ArgumentParser(description="Open Flamethrower for a project.")
    parser.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    parser.add_argument("--acp-port", type=int, help="Local Copilot ACP TCP port")
    args = parser.parse_args()
    if args.acp_port is not None and not 1 <= args.acp_port <= 65535:
        parser.error("--acp-port must be between 1 and 65535")
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        parser.error(f"Project directory does not exist: {project}")
    try:
        prepare_storage(project)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot prepare project storage: {error}\n")

    os.environ["FLAMETHROWER_PROJECT"] = str(project)
    os.environ["DJANGO_SETTINGS_MODULE"] = "flamethrower.settings"
    os.environ["FLAMETHROWER_ACP_PORT"] = str(args.acp_port or "")
    import django
    django.setup()
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application

    lock_path = project / ".flamethrower" / "app.lock"
    if lock_path.is_symlink():
        parser.exit(1, "Refusing symlinked application lock.\n")
    try:
        with portalocker.Lock(lock_path, mode="a", timeout=0):
            call_command("migrate", interactive=False, verbosity=0)
            from tasks.models import AgentQuestion, AgentRun
            from django.utils import timezone
            AgentRun.objects.filter(status__in=AgentRun.ACTIVE).update(
                status=AgentRun.Status.INTERRUPTED,
                error="Application stopped during this run. The agent may still be working; inspect before retrying.",
                finished_at=timezone.now(),
            )
            AgentQuestion.objects.filter(
                outcome=AgentQuestion.Outcome.PENDING, run__status=AgentRun.Status.INTERRUPTED,
            ).update(outcome=AgentQuestion.Outcome.CANCELLED)
            from tasks.similarity import recover_interrupted, start_worker
            recover_interrupted()
            application = StaticFilesHandler(get_wsgi_application())
            with make_server("0.0.0.0", 0, application) as server:
                from django.conf import settings
                settings.LOCAL_API_TOKEN = secrets.token_urlsafe(32)
                stop_worker = worker = None
                try:
                    with handle_termination(), runtime_record(project / ".flamethrower", server.server_port, settings.LOCAL_API_TOKEN):
                        stop_worker, worker = start_worker()
                        url = f"http://127.0.0.1:{server.server_port}/"
                        print(f"Project: {project}\nOpen: {url}\nPress Ctrl+C to stop.", flush=True)
                        if not args.no_browser and not webbrowser.open(url):
                            print("Could not open a browser; use the URL above.", file=sys.stderr)
                        try:
                            server.serve_forever()
                        except KeyboardInterrupt:
                            print("\nStopped. Active agent work may continue; inspect it before retrying.")
                finally:
                    if stop_worker is not None:
                        stop_worker.set()
                        worker.join()
    except portalocker.exceptions.LockException:
        parser.exit(1, f"Another Flamethrower instance is using {project}.\n")
