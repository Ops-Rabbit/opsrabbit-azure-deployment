"""Exercise direct command and file APIs in the pinned runtime container."""

import hashlib
import http.client
import json
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def apparmor_policy(name):
    """CI-only named profile: never attaches to a host executable by path.

    Rule classes follow upstream's bwrap-userns-restrict profile (ABI 5).
    Use ix, not pix: the trusted nested bwrap must inherit permission rather
    than stack the host's unpriv_bwrap capability denial under no-new-privs.
    This replaces the fixture's apparmor=unconfined, not runtime isolation.
    Payload namespace creation remains denied by the immutable seccomp filter.
    https://gitlab.com/apparmor/apparmor/-/blob/master/profiles/apparmor/profiles/extras/bwrap-userns-restrict
    https://manpages.ubuntu.com/manpages/resolute/man5/apparmor.d.5.html
    """
    if not name.startswith("skills156-ci-") or not name[13:].isalnum():
        raise ValueError("invalid fixture profile name")
    return f"""abi <abi/5.0>,
profile {name} flags=(attach_disconnected,mediate_deleted) {{
  allow capability,
  allow file rwlkm /{{**,}},
  allow ix /**,
  allow network,
  allow unix,
  allow ptrace,
  allow signal,
  allow mqueue,
  allow io_uring,
  allow userns create,
  allow mount,
  allow umount,
  allow pivot_root,
  allow dbus,
}}
"""


def policy_command(policy, *options):
    # Stdin avoids persistent host policy files/cache. -a never replaces an
    # existing profile. Compilation warnings are failures, not a fallback.
    # https://manpages.ubuntu.com/manpages/resolute/man8/apparmor_parser.8.html
    subprocess.run(
        ["sudo", "-n", "/usr/sbin/apparmor_parser", "-K", *options],
        input=policy,
        text=True,
        check=True,
        timeout=30,
    )


@contextmanager
def fixture_apparmor(enabled):
    if not enabled:
        yield "unconfined"
        return
    name = "skills156-ci-" + uuid.uuid4().hex
    policy = apparmor_policy(name)
    print("CI container AppArmor policy:\n" + policy, flush=True)
    policy_command(policy, "-Q", "--Werror")
    try:
        # Cleanup is attempted even after a timeout/ambiguous load failure.
        policy_command(policy, "-a", "--Werror")
        yield name
    finally:
        policy_command(policy, "-R")
        print("Removed CI AppArmor profile " + name, flush=True)


def host_prerequisites():
    """Read only; never change global sysctls, profiles, or kernel settings."""
    paths = (
        "/proc/sys/kernel/osrelease",
        "/proc/sys/kernel/unprivileged_userns_clone",
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
        "/proc/sys/kernel/apparmor_restrict_unprivileged_unconfined",
        "/proc/sys/user/max_user_namespaces",
        "/sys/module/apparmor/parameters/enabled",
    )
    evidence = {}
    for path in paths:
        try:
            evidence[path] = Path(path).read_text().strip()
        except OSError as error:
            evidence[path] = type(error).__name__
    print("Host namespace prerequisites: " + json.dumps(evidence), flush=True)


def container_prerequisites(container, expected_profile):
    inspected = json.loads(
        subprocess.check_output(["docker", "inspect", container], timeout=10)
    )[0]
    evidence = {
        "apparmor": inspected["AppArmorProfile"],
        "securityOptions": inspected["HostConfig"]["SecurityOpt"],
        "capAdd": inspected["HostConfig"]["CapAdd"],
        "user": inspected["Config"]["User"],
    }
    print("Container namespace prerequisites: " + json.dumps(evidence), flush=True)
    if expected_profile != "unconfined" and evidence["apparmor"] != expected_profile:
        raise RuntimeError("CI AppArmor profile was not attached to the container")
    # Read only safe kernel fields, never cmdline/environ. This observes the
    # trusted supervisor after its outer bwrap has dropped capabilities.
    script = """
import json
from pathlib import Path
fields = {'Name','Pid','PPid','Uid','Gid','CapInh','CapPrm','CapEff','CapBnd',
          'CapAmb','NoNewPrivs','Seccomp'}
for entry in sorted(Path('/proc').iterdir()):
    if not entry.name.isdecimal():
        continue
    try:
        status = dict(line.split(':',1) for line in (entry/'status').read_text().splitlines())
        result = {key:value.strip() for key,value in status.items() if key in fields}
        for name in ('attr/current','uid_map','gid_map'):
            try: result[name] = (entry/name).read_text().strip()
            except OSError as error: result[name] = type(error).__name__
        print(json.dumps(result), flush=True)
    except OSError:
        pass
"""
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "/usr/local/bin/python",
            "-I",
            "-S",
            "-c",
            script,
        ],
        check=True,
        timeout=15,
    )


def parse_event(line):
    """Pinned execd writes bare JSON records separated by blank lines.

    Also accept the SDK's data-prefixed SSE form, but never silently discard
    malformed records or an error event while interpreting a command result.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    event = json.loads(line)
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("invalid execd stream event")
    return event


def request(host, path, method="GET", body=None, headers=None, timeout=15):
    connection = http.client.HTTPConnection(host, 8080, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def run_command(
    host,
    token,
    *,
    command=None,
    argv=None,
    cwd="/workspace",
    envs=None,
    timeout=5000,
    succeeds=True,
):
    payload = {"cwd": cwd, "envs": envs or {}, "timeout": timeout, "background": False}
    payload.update({"command": command} if command is not None else {"argv": argv})
    status, body = request(
        host,
        "/command",
        "POST",
        json.dumps(payload),
        {
            "Content-Type": "application/json",
            "X-EXECD-ACCESS-TOKEN": token,
        },
        timeout=max(15, timeout / 1000 + 10),
    )
    events = []
    if status == 200:
        events = [
            event
            for line in body.splitlines()
            if (event := parse_event(line)) is not None
        ]
    errors = [event for event in events if event.get("type") == "error"]
    completed = any(event.get("type") == "execution_complete" for event in events)
    if succeeds and (status != 200 or errors or not completed):
        raise RuntimeError(
            f"workspace command did not complete successfully (HTTP {status}): "
            + repr(body[-3000:])
        )
    if not succeeds and (status != 200 or not errors):
        raise RuntimeError(
            "workspace command lacked the expected execution error event"
        )
    return "".join(
        event.get("text", "") for event in events if event.get("type") == "stdout"
    )


def adversarial_lifecycle_argv(marker, exit_after_fork=False):
    """Mutate payload lifecycle state; only trusted PID1 may own cleanup.

    Native argv deliberately avoids a shell retaining its own parent-death
    signal. A set-ID-marked workspace executable exercises exec with NNP,
    then both PID2 and its detached descendant attempt delayed writes.
    """
    after_exec = """
import ctypes, os, signal, time
libc = ctypes.CDLL(None, use_errno=True)
assert os.getpid() == 2 and os.getppid() == 1
assert os.getresuid() == (1000,1000,1000)
assert os.getresgid() == (1000,1000,1000)
assert libc.prctl(39,0,0,0,0) == 1  # PR_GET_NO_NEW_PRIVS
assert not os.path.exists('/proc/self/environ')
death_signal = ctypes.c_int(-1)
assert libc.prctl(2,ctypes.byref(death_signal),0,0,0) == 0
assert death_signal.value == 0
ready_read, ready_write = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(ready_read)
    os.setsid()
    assert libc.prctl(1,0,0,0,0) == 0
    os.write(ready_write,b'1')
    os.close(ready_write)
    time.sleep(3)
    open('/workspace/' + MARKER + '-survived-child','w').write('bad')
    os._exit(0)
os.close(ready_write)
assert os.read(ready_read,1) == b'1'
os.close(ready_read)
open('/workspace/' + MARKER + '-ready','w').write('ready')
print(MARKER + '-ready', flush=True)
if EXIT_AFTER_FORK:
    os._exit(0)
time.sleep(3)
open('/workspace/' + MARKER + '-survived','w').write('bad')
os.waitpid(pid,0)
"""
    after_exec = (
        f"MARKER={marker!r}\nEXIT_AFTER_FORK={exit_after_fork!r}\n" + after_exec
    )
    before_exec = """
import ctypes, errno, os, shutil, signal, sys
libc = ctypes.CDLL(None, use_errno=True)
assert os.getpid() == 2 and os.getppid() == 1
assert libc.prctl(39,0,0,0,0) == 1
assert libc.prctl(38,0,0,0,0) == -1  # NNP cannot be cleared
assert libc.prctl(39,0,0,0,0) == 1
# Both changing and clearing the payload's death signal are allowed: neither
# operation may change the trusted PID1 reaper's kernel state.
for sig in (0,signal.SIGTERM,signal.SIGKILL,0):
    assert libc.prctl(1,sig,0,0,0) == 0
os.setresuid(*os.getresuid())
os.setresgid(*os.getresgid())
for setter in (os.setuid,os.setgid):
    try: setter(0)
    # UID/GID 0 is unmapped in this user namespace (EINVAL), or denied by
    # dropped capabilities (EPERM). Both must leave every credential unchanged.
    except OSError as error: assert error.errno in (errno.EPERM,errno.EINVAL)
    else: raise AssertionError('payload changed to root')
for setter in (libc.setfsuid,libc.setfsgid):
    assert setter(-1) == 1000
    assert setter(0) == 1000
    assert setter(-1) == 1000
assert os.getresuid() == (1000,1000,1000)
assert os.getresgid() == (1000,1000,1000)
target = '/workspace/' + MARKER + '-exec'
shutil.copyfile(sys.executable,target)
os.chmod(target,0o6755)
assert os.stat(target).st_mode & 0o6000 == 0o6000
os.execv(target,[target,'-c',AFTER_EXEC])
"""
    code = f"MARKER={marker!r}\nAFTER_EXEC={after_exec!r}\n" + before_exec
    return ["/usr/local/bin/python", "-c", code]


def main(image, ci_apparmor=False):
    host_prerequisites()
    with fixture_apparmor(ci_apparmor) as profile:
        exercise_runtime(image, profile)


def exercise_runtime(image, profile):
    network = "skills156-runtime-" + uuid.uuid4().hex[:12]
    container = None
    try:
        subprocess.run(
            ["docker", "network", "create", "--internal", network],
            check=True,
            capture_output=True,
            text=True,
        )
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "-d",
                "--network",
                network,
                # Exercise Bubblewrap mount isolation on the ephemeral,
                # read-only-token CI runner; this is test-container-only.
                "--security-opt",
                "apparmor=" + profile,
                "--security-opt",
                "seccomp=unconfined",
                "--cap-add",
                "SYS_ADMIN",
                # Reach the container on its internal bridge address from the
                # runner instead of publishing a host port.
                image,
            ],
            text=True,
        ).strip()
        deadline = time.monotonic() + 30
        host = None
        inspected = None
        while time.monotonic() < deadline:
            inspected = json.loads(
                subprocess.check_output(["docker", "inspect", container])
            )[0]
            host = (
                inspected["NetworkSettings"]["Networks"]
                .get(network, {})
                .get("IPAddress")
            )
            if host:
                break
            state = inspected["State"]
            if not state["Running"]:
                logs = subprocess.check_output(["docker", "logs", container], text=True)
                raise RuntimeError(
                    "runtime container exited before publishing its port "
                    f"(exit={state['ExitCode']}, error={state['Error']!r}, "
                    f"oom={state['OOMKilled']}): {logs[-2000:]}"
                )
            time.sleep(0.1)
        if not host:
            raise RuntimeError(
                "runtime container did not join its isolated network within 30 seconds: "
                + json.dumps(
                    {
                        "state": inspected["State"],
                        "networks": inspected["NetworkSettings"]["Networks"],
                    },
                    sort_keys=True,
                )
            )
        for _ in range(120):
            state = json.loads(
                subprocess.check_output(["docker", "inspect", container])
            )[0]["State"]
            if not state["Running"]:
                logs = subprocess.check_output(
                    ["docker", "logs", container], stderr=subprocess.STDOUT, text=True
                )
                raise RuntimeError("runtime exited before readiness: " + logs[-2000:])
            try:
                status, _ = request(host, "/ping", timeout=3)
                if status == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("runtime gateway did not become ready")

        token = "synthetic-local-isolation-test-token"
        binding = {
            "sandboxId": "local-isolation-test",
            "generation": 1,
            "envs": {
                "LD_LIBRARY_PATH": "/usr/local/lib",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/workspace",
            },
            "accessTokenHash": "sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        }
        status, initialized = request(
            host,
            "/internal/init",
            "POST",
            json.dumps(binding),
            {"Content-Type": "application/json"},
        )
        if status != 200:
            raise RuntimeError(f"one-shot runtime binding failed with HTTP {status}")
        if (
            json.loads(initialized).get("runtimeContract")
            != "workspace-command-files-v1"
        ):
            raise RuntimeError("runtime did not advertise the enforced contract")
        container_prerequisites(container, profile)
        for endpoint in ("/code", "/session", "/pty"):
            status, _ = request(
                host,
                endpoint,
                "POST",
                "{}",
                {"Content-Type": "application/json", "X-EXECD-ACCESS-TOKEN": token},
            )
            if status != 404:
                raise RuntimeError("runtime exposed an alternate execution endpoint")

        command = (
            "test ! -e /proc/self/environ && test ! -e /etc/passwd && "
            'test -z "$IDENTITY_ENDPOINT" && ! touch /usr/forbidden 2>/dev/null && '
            "bash -c 'items=(one two); test ${#items[@]} -eq 2' && "
            "python -c 'import csv, json, ssl, sqlite3; assert sqlite3.sqlite_version' && "
            "test \"$(printf abc | sha256sum | cut -d' ' -f1)\" = "
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad && "
            "printf generated-by-command > /workspace/generated.txt && printf isolation-ok"
        )
        if run_command(host, token, command=command) != "isolation-ok":
            raise RuntimeError("isolated command did not produce its success marker")

        status, body = request(
            host,
            "/files/download?path=%2Fworkspace%2Fgenerated.txt",
            headers={"X-EXECD-ACCESS-TOKEN": token},
        )
        if status != 200 or body != b"generated-by-command":
            raise RuntimeError(
                f"command output file download failed with HTTP {status}"
            )
        # Same launch boundary for native argv, cwd and per-request environment.
        run_command(
            host,
            token,
            command="mkdir -p /workspace/sub; printf 'test ! -e /proc/self/environ || exit 91\n' > /workspace/startup",
        )
        native = run_command(
            host,
            token,
            argv=[
                "/bin/bash",
                "-c",
                'test ! -e /proc/self/environ && test "$PWD" = /workspace/sub && printf "%s" "$CANARY"',
            ],
            cwd="/workspace/sub",
            envs={
                "BASH_ENV": "/workspace/startup",
                "PYTHONPATH": "/workspace",
                "CANARY": "$(touch /workspace/parent-evaluated)",
                "PATH": "/workspace",
            },
        )
        if native != "$(touch /workspace/parent-evaluated)":
            raise RuntimeError("command environment was not forwarded literally")
        run_command(
            host,
            token,
            command="test ! -e /workspace/parent-evaluated && test ! -r /etc/shadow && test ! -w /usr && test -x /usr/bin/readlink && test -x /usr/bin/dirname",
        )
        run_command(
            host,
            token,
            command='set -eu; for d in .opsrabbit-system .opsrabbit-empty .opsrabbit-skills .opsrabbit-browser-runtime; do test ! -w /workspace/$d; ! touch /workspace/$d/injected 2>/dev/null; ! mv /workspace/$d /workspace/moved 2>/dev/null; done; for d in proc etc root home var data mnt media srv; do test ! -w /$d; done; printf stdin-ok > /workspace/.opsrabbit-runtime/stdin; test "$(cat /workspace/.opsrabbit-runtime/stdin)" = stdin-ok',
        )
        for name in (
            ".opsrabbit-system",
            ".opsrabbit-empty",
            ".opsrabbit-skills",
            ".opsrabbit-browser-runtime",
        ):
            status, _ = request(
                host,
                "/files/download?path=/workspace/" + name + "/canary",
                headers={"X-EXECD-ACCESS-TOKEN": token},
            )
            if status != 400:
                raise RuntimeError("file API accepted a reserved path")
        run_command(
            host,
            token,
            command="ln -s /workspace/.opsrabbit-system /workspace/reserved-link; ln -s /proc /workspace/proc-link; test ! -e /workspace/proc-link/self/environ",
        )
        status, _ = request(
            host,
            "/files/download?path=/workspace/reserved-link/canary",
            headers={"X-EXECD-ACCESS-TOKEN": token},
        )
        if status != 400:
            raise RuntimeError("file API accepted a reserved alias")
        # Neither alternate cwd nor another namespace may reveal parent procfs.
        run_command(
            host, token, command="test ! -e self/environ", cwd="/workspace/proc-link"
        )
        # Probe the kernel filter directly: a nested bwrap failure alone could
        # merely demonstrate absent procfs, not a blocked namespace syscall.
        filter_output = run_command(
            host,
            token,
            argv=[
                "/usr/local/bin/python",
                "-c",
                """
import ctypes, errno, os, platform, threading
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
numbers = {'x86_64': (56,272,308,101,311), 'aarch64': (220,97,268,117,271)}
clone, unshare, setns, ptrace, process_vm_writev = numbers[platform.machine()]
def denied(number, expected, *args):
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(number), *[ctypes.c_long(a) for a in args])
    assert result == -1 and ctypes.get_errno() == expected, (number,result,ctypes.get_errno())
denied(unshare, errno.EPERM, 0)
denied(setns, errno.EPERM, -1, 0)
denied(435, errno.ENOSYS, 0, 0)
# Protect the same-UID trusted reaper independently of the host's Yama policy.
denied(ptrace, errno.EPERM, -1, -1, 0, 0)
denied(ptrace, errno.EPERM, 0x4206, 1, 0, 0)  # PTRACE_SEIZE PID1
denied(process_vm_writev, errno.EPERM, 1, 0, 0, 0, 0, 0)
for flag in (0x80,0x20000,0x2000000,0x4000000,0x8000000,0x10000000,0x20000000,0x40000000):
    denied(clone, errno.EPERM, flag | 17, 0, 0, 0, 0)
if platform.machine() == 'x86_64':
    denied(0x40000000 | unshare, errno.ENOSYS, 0)
completed = []
thread = threading.Thread(target=lambda: completed.append(True))
thread.start(); thread.join(); assert completed == [True]
pid = os.fork()
if pid == 0: os._exit(0)
assert os.waitpid(pid,0)[1] == 0
print('namespace-filter-ok')
""",
            ],
        )
        if filter_output != "namespace-filter-ok\n":
            raise RuntimeError("namespace filter probe did not complete")
        run_command(
            host,
            token,
            argv=[
                "/usr/bin/bwrap",
                "--unshare-user",
                "--proc",
                "/proc",
                "--",
                "/bin/sh",
                "-c",
                "cat /proc/self/environ",
            ],
            succeeds=False,
        )
        # Raw workers are loopback-only and command children have no access to
        # the parent's loopback listeners, even if a synthetic token is known.
        for port in (8081, 8082):
            connection = http.client.HTTPConnection(host, port, timeout=2)
            try:
                connection.request("GET", "/ping")
                connection.getresponse()
            except (OSError, http.client.HTTPException):
                pass
            else:
                raise RuntimeError("raw execd worker was exposed")
            finally:
                connection.close()
        run_command(
            host,
            token,
            command="python - <<'PY'\nimport socket\nfor port in (8080,8081,8082):\n s=socket.socket(); s.settimeout(1)\n assert s.connect_ex(('127.0.0.1',port)) != 0\n s.close()\nPY",
        )
        # The trusted PID1 destroys orphan grandchildren after PID2 exits,
        # even when the payload clears PDEATHSIG and executes a set-ID file.
        run_command(
            host,
            token,
            argv=adversarial_lifecycle_argv("orphan", exit_after_fork=True),
        )
        run_command(
            host,
            token,
            command="sleep 4; test -e /workspace/orphan-ready && test ! -e /workspace/orphan-survived-child",
        )
        run_command(
            host,
            token,
            argv=adversarial_lifecycle_argv("timeout"),
            timeout=1500,
            succeeds=False,
        )
        run_command(
            host,
            token,
            command="sleep 4; test -e /workspace/timeout-ready && test ! -e /workspace/timeout-survived && test ! -e /workspace/timeout-survived-child",
        )
        # Background execution returns before completion, but must use the
        # identical boundary and keep cancellation of its whole PID namespace.
        status, body = request(
            host,
            "/command",
            "POST",
            json.dumps(
                {
                    "argv": adversarial_lifecycle_argv("background"),
                    "background": True,
                    "timeout": 10000,
                }
            ),
            {"Content-Type": "application/json", "X-EXECD-ACCESS-TOKEN": token},
        )
        events = [
            event
            for line in body.splitlines()
            if (event := parse_event(line)) is not None
        ]
        if status != 200 or any(event["type"] == "error" for event in events):
            raise RuntimeError("background launch failed")
        execution_id = next(
            event["text"] for event in events if event["type"] == "init"
        )
        run_command(
            host,
            token,
            command="for i in $(seq 1 30); do test -e /workspace/background-ready && exit 0; sleep 0.05; done; exit 1",
        )
        status, body = request(
            host,
            "/command/status/" + execution_id,
            headers={"X-EXECD-ACCESS-TOKEN": token},
        )
        if status != 200 or json.loads(body).get("running") is not True:
            raise RuntimeError("background command was not tracked while running")
        status, _ = request(
            host,
            "/command?id=" + execution_id,
            "DELETE",
            headers={"X-EXECD-ACCESS-TOKEN": token},
        )
        if status != 200:
            raise RuntimeError("background cancellation failed")
        run_command(
            host,
            token,
            command="sleep 4; test ! -e /workspace/background-survived && test ! -e /workspace/background-survived-child",
        )
        status, body = request(
            host,
            "/command/status/" + execution_id,
            headers={"X-EXECD-ACCESS-TOKEN": token},
        )
        if status != 200 or json.loads(body).get("running") is not False:
            raise RuntimeError("cancelled background command stayed running")
        # A foreground stream disconnect must cancel its command namespace.
        stream = http.client.HTTPConnection(host, 8080, timeout=15)
        stream.request(
            "POST",
            "/command",
            json.dumps(
                {
                    "argv": adversarial_lifecycle_argv("disconnect"),
                    "timeout": 10000,
                    "background": False,
                }
            ),
            {"Content-Type": "application/json", "X-EXECD-ACCESS-TOKEN": token},
        )
        response = stream.getresponse()
        while True:
            line = response.readline()
            if not line:
                raise RuntimeError("command ended before disconnect fixture was ready")
            event = parse_event(line)
            if event is not None:
                if event.get("type") == "stdout" and "disconnect-ready" in event.get(
                    "text", ""
                ):
                    break
        response.close()
        stream.close()
        run_command(
            host,
            token,
            command="sleep 4; test ! -e /workspace/disconnect-survived && test ! -e /workspace/disconnect-survived-child",
        )
        print("Native workspace command/file contract, isolation and cleanup: PASS")
    finally:
        try:
            if container is not None:
                subprocess.run(
                    ["docker", "rm", "-f", container],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
        finally:
            subprocess.run(
                ["docker", "network", "rm", network],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (
        len(sys.argv) == 3 and sys.argv[2] != "--ci-apparmor"
    ):
        raise SystemExit("usage: test-runtime-container.py <image> [--ci-apparmor]")
    main(sys.argv[1], ci_apparmor=len(sys.argv) == 3)
