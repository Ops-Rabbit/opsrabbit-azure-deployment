"""Launch each execd API worker inside the same workspace-only boundary."""

import os
import stat
import sys


def ensure_workspace_tmp(path="/workspace/.tmp"):
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise RuntimeError("workspace temporary path is not a directory")


def sandbox_args(port):
    if port == "8082":
        return [
            "bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--dir",
            "/opt",
            "--ro-bind",
            "/opt/execd",
            "/opt/execd",
            "--bind",
            "/workspace",
            "/workspace",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/workspace/.tmp",
            "--chdir",
            "/workspace",
            "--clearenv",
            "--setenv",
            "HOME",
            "/workspace",
            "--setenv",
            "TMPDIR",
            "/workspace/.tmp",
            "--",
            "/opt/execd",
            "--runtime-init",
            "--runtime-init-once",
            "--port",
            port,
            "--log-level",
            "3",
        ]

    args = [
        "bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
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
        "/workspace",
        "/workspace",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/workspace/.tmp",
        "--dev",
        "/dev",
        "--chdir",
        "/workspace",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "LD_LIBRARY_PATH",
        "/usr/local/lib",
        "--setenv",
        "HOME",
        "/workspace",
        "--setenv",
        "TMPDIR",
        "/workspace/.tmp",
        "--",
    ]
    return args + [
        "/opt/execd",
        "--runtime-init",
        "--runtime-init-once",
        "--port",
        port,
        "--log-level",
        "3",
    ]


def main():
    if len(sys.argv) == 1:
        os.chdir("/workspace")
        os.execve(
            "/usr/local/bin/python",
            ["python", "-I", "/opt/runtime-gateway.py"],
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LD_LIBRARY_PATH": "/usr/local/lib",
                "HOME": "/workspace",
            },
        )
        return
    elif len(sys.argv) == 2 and sys.argv[1] == "command":
        port = "8081"
    elif len(sys.argv) == 2 and sys.argv[1] == "files":
        port = "8082"
    else:
        raise SystemExit("usage: runtime-start.py [command|files]")
    os.chdir("/workspace")
    ensure_workspace_tmp()
    os.execvp("bwrap", sandbox_args(port))


if __name__ == "__main__":
    main()
