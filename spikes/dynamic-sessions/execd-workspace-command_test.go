package runtime

import (
    "context"
    "encoding/json"
    "errors"
    "reflect"
    "testing"
)

func TestWorkspaceCommandFixedLauncherForShellAndArgv(t *testing.T) {
    t.Setenv("CUSTOMER_SECRET", "parent-only-canary")
    t.Setenv("BASH_ENV", "/workspace/parent-startup")
    for _, req := range []*ExecuteCodeRequest{
        {Code: "printf '%s' \"$(cat /proc/self/environ)\"", Cwd: "/workspace"},
        {Argv: []string{"/workspace/tool", "$(touch /tmp/parent)"}, Cwd: "/workspace"},
    } {
        req.Envs = map[string]string{
            "LD_PRELOAD": "/workspace/inject.so", "PYTHONPATH": "/workspace",
            "BASH_ENV": "/workspace/startup", "PATH": "/workspace",
        }
        cmd, err := prepareCommand(context.Background(), req)
        if err != nil { t.Fatal(err) }
        if cmd.Path != "/usr/local/bin/python" || cmd.Dir != "/" {
            t.Fatalf("request changed parent executable or cwd: %q %q", cmd.Path, cmd.Dir)
        }
        if len(cmd.Args) != 5 || !reflect.DeepEqual(cmd.Args[:4], []string{
            "/usr/local/bin/python", "-I", "-S", "/opt/workspace-command.py",
        }) { t.Fatal("unsafe launcher arguments") }
        expectedEnv := []string{"PATH=/usr/local/bin:/usr/bin:/bin", "LD_LIBRARY_PATH=/usr/local/lib", "HOME=/workspace"}
        if !reflect.DeepEqual(cmd.Env, expectedEnv) { t.Fatalf("parent environment changed: %v", cmd.Env) }
        var envelope struct {
            Command string `json:"command"`
            Argv []string `json:"argv"`
            Cwd string `json:"cwd"`
            Envs map[string]string `json:"envs"`
        }
        if err := json.Unmarshal([]byte(cmd.Args[4]), &envelope); err != nil { t.Fatal(err) }
        if envelope.Command != req.Code || !reflect.DeepEqual(envelope.Argv, req.Argv) ||
            !reflect.DeepEqual(envelope.Envs, req.Envs) || envelope.Cwd != req.Cwd {
            t.Fatal("request was evaluated or altered instead of encoded for the child")
        }
    }
}

func TestWorkspaceCommandRejectsParentIdentityAndCwd(t *testing.T) {
    uid := uint32(0)
    for _, req := range []*ExecuteCodeRequest{
        {Code: "true", Uid: &uid}, {Code: "true", Gid: &uid},
        {Code: "true", Cwd: "/proc"}, {Code: "true", Cwd: "/workspace/../proc"},
        {Code: "true", Cwd: "$HOME"}, {Code: "true", Cwd: "relative"},
    } {
        if _, err := prepareCommand(context.Background(), req); err == nil {
            t.Fatalf("unsafe request accepted: %+v", req)
        }
    }
}

func TestWorkspaceCommandEmptyEnvironmentIsObject(t *testing.T) {
    cmd, err := prepareCommand(context.Background(), &ExecuteCodeRequest{Code: "true"})
    if err != nil { t.Fatal(err) }
    var envelope map[string]any
    if err := json.Unmarshal([]byte(cmd.Args[4]), &envelope); err != nil { t.Fatal(err) }
    if env, ok := envelope["envs"].(map[string]any); !ok || len(env) != 0 {
        t.Fatal("omitted command env must be an empty object, never inherited")
    }
}

func TestWorkspaceCommandCancellationBeforeLaunch(t *testing.T) {
    ctx, cancel := context.WithCancel(context.Background())
    cmd, err := prepareCommand(ctx, &ExecuteCodeRequest{Code: "touch /tmp/must-not-run"})
    if err != nil { t.Fatal(err) }
    cancel()
    if err := cmd.Start(); !errors.Is(err, context.Canceled) {
        t.Fatalf("canceled launch returned %v", err)
    }
}
