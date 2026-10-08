"""Subscription CLIs (Claude Code, Codex) as model providers, using the login you
already have instead of an API key. Neither CLI is bundled: they are found on PATH,
in the usual install folders, or (Windows) inside WSL. Adapted from LocalFlow."""
import glob
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

log = logging.getLogger(__name__)
IS_WIN = sys.platform == "win32"


def _candidates(name):
    home = os.path.expanduser("~")
    if IS_WIN:
        appdata = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        return [os.path.join(home, ".local", "bin", name + ".exe"),
                os.path.join(appdata, "npm", name + ".cmd"),
                os.path.join(local, "Programs", name, name + ".exe"),
                os.path.join(home, ".claude", "local", name + ".exe")]
    paths = [os.path.join(home, ".local", "bin", name), "/opt/homebrew/bin/" + name,
             "/usr/local/bin/" + name, os.path.join(home, ".claude", "local", name),
             os.path.join(home, ".npm-global", "bin", name), os.path.join(home, ".bun", "bin", name)]
    paths += sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin", name)),
                    reverse=True)
    return paths


_found = {}


def find_cli(name, override=""):
    """Command prefix for a CLI: the saved override, else the first install found."""
    if override:
        return shlex.split(override, posix=not IS_WIN)
    if name not in _found:
        options = candidates(name)
        _found[name] = options[0] if options else None
    return _found[name]


def wsl_distros():
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        names = out.decode("utf-16-le", "ignore").replace("\x00", "").split()
    except Exception:
        return []
    return [n for n in names if n and not n.lower().startswith("docker-desktop")]


def candidates(name):
    """Every install of ``name`` we can find, as command prefixes, best guess first."""
    found = []
    path = shutil.which(name)
    if path:
        found.append([path])
    for path in _candidates(name):
        if os.path.isfile(path) and [path] not in found:
            found.append([path])
    if IS_WIN and shutil.which("wsl.exe"):
        for distro in wsl_distros():
            prefix = ["wsl.exe", "-d", distro, "-e", "bash", "-lc"]
            try:
                probe = subprocess.run(prefix + ["command -v " + name], capture_output=True, text=True,
                                       timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
                if probe.returncode == 0 and probe.stdout.strip():
                    found.append(prefix)  # args are appended as one shell string
            except Exception:
                continue
    return found


def prefix_to_text(prefix):
    return subprocess.list2cmdline(prefix) if IS_WIN else " ".join(shlex.quote(p) for p in prefix)


INSTALL_URLS = {"claude": "https://code.claude.com/docs/en/setup",
                "codex": "https://developers.openai.com/codex/cli"}
LOGIN_ARGS = {"claude": [], "codex": ["login"]}  # `claude` alone walks you through sign-in


def classify(error):
    """Turn a CLI failure into a status the setup screen can explain."""
    text = str(error).lower()
    if "did not answer" in text:
        return "timeout"
    if any(k in text for k in ("not logged in", "/login", "log in", "login", "authenticat", "401",
                                "unauthorized", "credentials")):
        return "signed_out"
    return "error"


def login_command(name, prefix):
    """argv that opens a visible terminal running the CLI's sign-in."""
    args = LOGIN_ARGS[name]
    if IS_WIN:
        if prefix[:1] == ["wsl.exe"]:
            inner = prefix + [" ".join([name] + args)]
        else:
            inner = prefix + args
        return ["cmd.exe", "/c", "start", "Activity Tracker sign-in", "cmd.exe", "/k"] + inner
    shell = " ".join(shlex.quote(p) for p in prefix + args)
    if sys.platform == "darwin":
        return ["osascript", "-e", 'tell application "Terminal" to do script "%s"' % shell.replace('"', '\\"'),
                "-e", 'tell application "Terminal" to activate']
    return ["x-terminal-emulator", "-e", shell]


def open_login(name, prefix):
    subprocess.Popen(login_command(name, prefix))


def _env_with_path(prefix):
    """Apps launched from Finder/Explorer get a minimal PATH; npm-installed CLIs need node."""
    env = dict(os.environ)
    extra = [os.path.dirname(prefix[0])] if os.path.isabs(prefix[0]) else []
    if not IS_WIN:
        extra += ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def _run(prefix, name, args, stdin_text, timeout):
    if prefix[:1] == ["wsl.exe"]:
        cmd = prefix + [" ".join(shlex.quote(a) for a in [name] + args)]
    else:
        cmd = prefix + args
    kwargs = {"env": _env_with_path(prefix)}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cwd:  # no project CLAUDE.md/AGENTS.md
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", cwd=cwd, **kwargs)
        try:
            out, err = proc.communicate(stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise RuntimeError("%s did not answer within %ds" % (name, timeout))
    if proc.returncode != 0:
        raise RuntimeError("%s exited %d: %s" % (name, proc.returncode, (err or out).strip()[-300:]))
    return out.strip()


def _kill_tree(proc):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def to_cli_path(prefix, path):
    """A Windows path as the CLI sees it (a CLI inside WSL needs /mnt/c/...)."""
    if prefix[:1] == ["wsl.exe"] and len(path) > 2 and path[1] == ":":
        return "/mnt/%s%s" % (path[0].lower(), path[2:].replace("\\", "/"))
    return path


def claude_args(system, model):
    args = ["-p", "--no-session-persistence", "--tools", "", "--setting-sources", "",
            "--strict-mcp-config", "--disable-slash-commands", "--output-format", "text",
            "--system-prompt", system]
    if model:
        args += ["--model", model]
    return args


def codex_args(prompt, model, image=None, out_file=None):
    args = ["exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "-s", "read-only",
            "-c", 'model_reasoning_effort="low"']
    if model:
        args += ["-m", model]
    if image:
        args += ["-i", image]
    if out_file:
        args += ["--output-last-message", out_file]
    return args + [prompt]


def run_claude(cfg, system, user, timeout):
    prefix = find_cli("claude", cfg.get("claude_path", ""))
    if not prefix:
        raise RuntimeError("Claude Code (claude) not found; install it and sign in once")
    return _run(prefix, "claude", claude_args(system, cfg.get("claude_model") or "haiku"), user, timeout)


def run_codex(cfg, prompt, stdin_text="", image=None, timeout=120):
    """Codex prints session noise around its answer, so read the answer from
    --output-last-message instead of stdout."""
    prefix = find_cli("codex", cfg.get("codex_path", ""))
    if not prefix:
        raise RuntimeError("Codex CLI (codex) not found; install it and run `codex login`")
    fd, out_file = tempfile.mkstemp(prefix="activitytracker-codex-", suffix=".txt")
    os.close(fd)
    try:
        args = codex_args(prompt, cfg.get("codex_model"), to_cli_path(prefix, image) if image else None,
                          to_cli_path(prefix, out_file))
        _run(prefix, "codex", args, stdin_text, timeout)
        with open(out_file, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass


def check(name, cfg, timeout=60):
    """Tiny round trip through the CLI. -> (ok, detail)."""
    try:
        if name == "claude":
            out = run_claude(cfg, "Reply with the single word: ready", "ping", timeout)
        else:
            out = run_codex(cfg, "Reply with the single word: ready", timeout=timeout)
        return True, out[:80]
    except Exception as exc:
        return False, "%s (%s)" % (str(exc)[:200], classify(exc))
