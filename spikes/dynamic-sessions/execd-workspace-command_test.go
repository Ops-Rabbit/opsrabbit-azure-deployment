package runtime

import (
    "bytes"
    "context"
    "crypto/sha256"
    "encoding/json"
    "errors"
    "fmt"
    "io"
    "os"
    "os/exec"
    "reflect"
    "strings"
    "testing"
    "time"
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
        t.Cleanup(func() { closeCommandExtraFiles(cmd) })
        if !reflect.DeepEqual(cmd.Args, []string{
            "/usr/local/bin/python", "-I", "-S", "/opt/workspace-command.py", "--request-fd", "3",
        }) { t.Fatal("unsafe launcher arguments") }
        if cmd.Stdin != nil { t.Fatal("request transport must not replace command stdin") }
        expectedEnv := []string{"PATH=/usr/local/bin:/usr/bin:/bin", "LD_LIBRARY_PATH=/usr/local/lib", "HOME=/workspace"}
        if !reflect.DeepEqual(cmd.Env, expectedEnv) { t.Fatalf("parent environment changed: %v", cmd.Env) }
        var envelope struct {
            Command string `json:"command"`
            Argv []string `json:"argv"`
            Cwd string `json:"cwd"`
            Envs map[string]string `json:"envs"`
        }
        if err := json.Unmarshal(workspaceEnvelopeBytes(t, cmd), &envelope); err != nil { t.Fatal(err) }
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
    t.Cleanup(func() { closeCommandExtraFiles(cmd) })
    var envelope map[string]any
    if err := json.Unmarshal(workspaceEnvelopeBytes(t, cmd), &envelope); err != nil { t.Fatal(err) }
    if env, ok := envelope["envs"].(map[string]any); !ok || len(env) != 0 {
        t.Fatal("omitted command env must be an empty object, never inherited")
    }
}

func TestWorkspaceCommandCancellationBeforeLaunch(t *testing.T) {
    ctx, cancel := context.WithCancel(context.Background())
    cmd, err := prepareCommand(ctx, &ExecuteCodeRequest{Code: "touch /tmp/must-not-run"})
    if err != nil { t.Fatal(err) }
    t.Cleanup(func() { closeCommandExtraFiles(cmd) })
    envelope := cmd.ExtraFiles[0]
    cancel()
    if _, err := launchWorkspaceCommand(cmd); !errors.Is(err, context.Canceled) {
        t.Fatalf("canceled launch returned %v", err)
    }
    assertWorkspaceEnvelopeClosed(t, cmd, envelope)
}

// Read the actual transport rather than extracting request data from argv.
func workspaceEnvelopeBytes(t *testing.T, cmd *exec.Cmd) []byte {
    t.Helper()
    if len(cmd.ExtraFiles) != 1 { t.Fatal("expected one request descriptor") }
    file := cmd.ExtraFiles[0]
    if _, err := os.Stat(file.Name()); !errors.Is(err, os.ErrNotExist) {
        t.Fatal("request envelope pathname survived preparation")
    }
    info, err := file.Stat()
    if err != nil { t.Fatal(err) }
    if info.Mode().Perm() != 0600 { t.Fatal("request envelope is not private") }
    payload, err := io.ReadAll(file)
    if err != nil { t.Fatal(err) }
    if _, err := file.Seek(0, 0); err != nil { t.Fatal(err) }
    return payload
}

func assertWorkspaceEnvelopeClosed(t *testing.T, cmd *exec.Cmd, file *os.File) {
    t.Helper()
    if len(cmd.ExtraFiles) != 0 { t.Fatal("parent retained inherited descriptors") }
    if _, err := file.Stat(); !errors.Is(err, os.ErrClosed) {
        t.Fatal("parent request descriptor was not closed")
    }
}

func TestWorkspaceCommandFailedStartClosesEnvelope(t *testing.T) {
    cmd, err := prepareCommand(context.Background(), &ExecuteCodeRequest{Code: "true"})
    if err != nil { t.Fatal(err) }
    t.Cleanup(func() { closeCommandExtraFiles(cmd) })
    envelope := cmd.ExtraFiles[0]
    cmd.Path = "/missing-opsrabbit-workspace-test/executable"
    if _, err := launchWorkspaceCommand(cmd); err == nil { t.Fatal("missing executable started") }
    assertWorkspaceEnvelopeClosed(t, cmd, envelope)
}

func TestWorkspaceCommandLargeEnvelopePreservesStdin(t *testing.T) {
    // Aggregate JSON exceeds MAX_ARG_STRLEN; no individual final arg/env does.
    // The Docker Go stage need not contain Python: a test child reads fd 3.
    executable, err := os.Executable()
    if err != nil { t.Fatal(err) }
    for _, req := range []*ExecuteCodeRequest{
        {Code: "printf ready", Envs: map[string]string{
            "PAD_A": strings.Repeat("a", 70*1024), "PAD_B": strings.Repeat("b", 70*1024),
        }},
        {Argv: []string{"/bin/echo", strings.Repeat("a", 70*1024), strings.Repeat("b", 70*1024)}},
    } {
        ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
        t.Cleanup(cancel)
        cmd, err := prepareCommand(ctx, req)
        if err != nil { t.Fatal(err) }
        t.Cleanup(func() { closeCommandExtraFiles(cmd) })
        payload := workspaceEnvelopeBytes(t, cmd)
        if len(payload) <= 128*1024 { t.Fatal("fixture does not reach the argv regression") }
        expected := fmt.Sprintf("%x\nstdin-canary", sha256.Sum256(payload))
        envelope := cmd.ExtraFiles[0]
        cmd.Path = executable
        cmd.Args = []string{executable, "-test.run=^TestWorkspaceCommandDescriptorChild$"}
        cmd.Env = append(cmd.Env, "OPSRABBIT_DESCRIPTOR_TEST_CHILD=1")
        cmd.Stdin = strings.NewReader("stdin-canary")
        var output bytes.Buffer
        cmd.Stdout = &output
        cmd.Stderr = &output
        process, err := launchWorkspaceCommand(cmd)
        if err != nil { t.Fatal(err) }
        assertWorkspaceEnvelopeClosed(t, cmd, envelope)
        if err := process.Wait(); err != nil { t.Fatal(err) }
        cancel()
        if output.String() != expected { t.Fatal("request or command stdin changed across exec") }
    }
}

func TestWorkspaceCommandDescriptorChild(t *testing.T) {
    if os.Getenv("OPSRABBIT_DESCRIPTOR_TEST_CHILD") != "1" { return }
    file := os.NewFile(3, "request")
    payload, err := io.ReadAll(file)
    if err != nil { os.Exit(2) }
    if err := file.Close(); err != nil { os.Exit(3) }
    stdin, err := io.ReadAll(os.Stdin)
    if err != nil { os.Exit(4) }
    fmt.Printf("%x\n%s", sha256.Sum256(payload), stdin)
    os.Exit(0)
}
