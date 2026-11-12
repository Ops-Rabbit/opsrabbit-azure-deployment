"""Immutable execd launch boundary; request data is interpreted only in the child.

Invoked by the patched execd with a fixed interpreter, isolated Python startup,
and a clean environment. No shell runs in the supervisor's procfs namespace.
"""

import json
import os
import platform
import posixpath
import re
import struct
import sys

RUNTIME_CONTRACT = "workspace-command-files-v1"
WORKSPACE = "/workspace"
EMPTY = "/opt/opsrabbit-system/empty"
NAMESPACE_FILTER = "/opt/opsrabbit-system/namespace-filter.bpf"
PROTECTED_NAMES = (
    ".opsrabbit-system",
    ".opsrabbit-empty",
    ".opsrabbit-skills",
    ".opsrabbit-browser-runtime",
)
LAUNCHER_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LD_LIBRARY_PATH": "/usr/local/lib",
    "HOME": WORKSPACE,
}
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def namespace_filter(machine=None):
    """Build the immutable, native-ABI seccomp program installed by bwrap.

    Block namespace creation/join without writing procfs sysctls. clone3's
    indirect flags cannot be inspected by classic BPF, so ENOSYS requests
    libc's legacy-clone fallback. Ordinary fork/threads remain supported.
    Foreign ABIs fail closed, including x86's x32 syscall-number encoding.
    Cross-process writes/tracing cannot modify the trusted PID1 reaper.
    """
    architecture = machine or platform.machine()
    if architecture == "x86_64":
        audit_arch, clone, unshare, setns = 0xC000003E, 56, 272, 308
        # Include legacy x32 aliases accepted without the ABI bit by old kernels.
        process_mutation = (101, 311, 521, 540)
    elif architecture == "aarch64":
        audit_arch, clone, unshare, setns = 0xC00000B7, 220, 97, 268
        process_mutation = (117, 271)
    else:
        raise ValueError("unsupported seccomp architecture")
    # Linux errno values, also when inspecting/generating the filter on macOS.
    deny = 0x00050000 | 1  # EPERM
    unavailable = 0x00050000 | 38  # ENOSYS
    instructions = [
        (0x20, 0, 0, 4),  # seccomp_data.arch
        (0x15, 1, 0, audit_arch),
        (0x06, 0, 0, 0x80000000),  # KILL_PROCESS for another ABI
        (0x20, 0, 0, 0),  # seccomp_data.nr
        (0x45, 0, 1, 0x40000000),  # x32, not covered by the native table
        (0x06, 0, 0, unavailable),
    ]
    for syscall in (unshare, setns, *process_mutation):
        instructions += [(0x15, 0, 1, syscall), (0x06, 0, 0, deny)]
    instructions += [
        (0x15, 0, 1, 435),  # clone3 on both supported architectures
        (0x06, 0, 0, unavailable),
        (0x15, 0, 3, clone),
        (0x20, 0, 0, 16),  # low word of args[0], clone flags
        (0x45, 0, 1, 0x7E020080),  # every CLONE_NEW* flag, including NEWTIME
        (0x06, 0, 0, deny),
        (0x06, 0, 0, 0x7FFF0000),  # ALLOW
    ]
    return b"".join(struct.pack("<HBBI", *instruction) for instruction in instructions)


def command_args(request, seccomp_fd):
    if type(seccomp_fd) is not int or seccomp_fd < 3:
        raise ValueError("a trusted seccomp descriptor is required")
    if not isinstance(request, dict) or set(request) != {
        "command",
        "argv",
        "cwd",
        "envs",
    }:
        raise ValueError("invalid workspace command envelope")
    cwd = request["cwd"] or WORKSPACE
    if not isinstance(cwd, str) or "\x00" in cwd or not cwd.startswith("/"):
        raise ValueError("command cwd must be an absolute workspace path")
    cwd = posixpath.normpath(cwd)
    if cwd != WORKSPACE and not cwd.startswith(WORKSPACE + "/"):
        raise ValueError("command cwd must stay inside /workspace")
    if cwd.split("/")[2:3] and cwd.split("/")[2] in PROTECTED_NAMES:
        raise ValueError("command cwd is reserved")
    # Do not resolve cwd or look up request executables in the parent. bwrap
    # changes cwd after constructing the child root, so symlink races cannot
    # retain a directory descriptor into the supervisor's /proc or /tmp.
    code, argv, envs = request["command"], request["argv"], request["envs"]
    if not isinstance(code, str) or "\x00" in code:
        raise ValueError("invalid command")
    if argv is None:
        if not code:
            raise ValueError("command is required")
        child = ["/bin/bash", "--noprofile", "--norc", "-c", code]
    else:
        if code or not isinstance(argv, list) or not argv or not argv[0]:
            raise ValueError("exactly one command or argv is required")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise ValueError("invalid argv")
        child = argv
    if not isinstance(envs, dict):
        raise ValueError("invalid command environment")
    for key, value in envs.items():
        if not ENV_NAME.fullmatch(key) or not isinstance(value, str) or "\x00" in value:
            raise ValueError("invalid command environment")

    args = [
        "/usr/bin/bwrap",
        "--unshare-user",
        "--seccomp",
        str(seccomp_fd),
        "--unshare-pid",
        # Keep bwrap's trusted PID1 reaper. The payload is PID2: its prctl,
        # credential changes and execs cannot clear PID1's parent-death signal.
        # When the monitor dies, PID1 dies and the kernel kills the namespace.
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/usr/local",
        "/usr/local",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        "/opt",
        "/opt",
        "--bind",
        WORKSPACE,
        WORKSPACE,
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/workspace/.tmp",
        "--dev",
        "/dev",
    ]
    for name in PROTECTED_NAMES:
        args += ["--ro-bind", EMPTY, WORKSPACE + "/" + name]
    # .opsrabbit-runtime stays in the workspace bind for product stdin files.
    # No proc mount, identity files, customer configuration, or host homes.
    for dest in (
        "/proc",
        "/etc",
        "/root",
        "/home",
        "/var",
        "/data",
        "/mnt",
        "/media",
        "/srv",
    ):
        args += ["--ro-bind", EMPTY, dest]
    args += ["--clearenv"]
    child_env = {
        **LAUNCHER_ENV,
        "TMPDIR": "/workspace/.tmp",
        "LANG": "C.UTF-8",
        **envs,
        # Match the product's immutable tool search path. A request cannot
        # redirect the executable selected for the initial native argv.
        "PATH": LAUNCHER_ENV["PATH"],
    }
    for key, value in child_env.items():
        args += ["--setenv", key, value]
    return args + ["--chdir", cwd, "--", *child]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("workspace launcher requires one request envelope")
    # The root-owned filter lives on the supervisor's immutable /opt mount.
    # Only this descriptor crosses exec; bwrap installs it after setup and
    # closes it before running the payload. Failure has no unfiltered fallback.
    fd = os.open(NAMESPACE_FILTER, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            args = command_args(json.loads(sys.argv[1]), fd)
        except (ValueError, TypeError):
            raise SystemExit("invalid workspace command request") from None
        os.set_inheritable(fd, True)
        # Replacing this process preserves execd's cancellation PID and group.
        os.execve(args[0], args, LAUNCHER_ENV)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
