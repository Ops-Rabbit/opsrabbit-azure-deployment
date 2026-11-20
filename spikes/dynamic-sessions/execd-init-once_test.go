// Copyright 2026 OpsRabbit contributors. Licensed under Apache-2.0.
package controller

import (
	"errors"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/alibaba/opensandbox/execd/pkg/binding"
	"github.com/alibaba/opensandbox/execd/pkg/web/model"
	"github.com/stretchr/testify/require"
)

func oneShotRequest() *model.RuntimeInitRequest {
	r := validInitRequest()
	r.AccessTokenHash = "sha256:" + strings.Repeat("a", 64)
	return r
}

func TestOneShotRejectsRebinding(t *testing.T) {
	prev := binding.Apply(nil)
	t.Cleanup(func() { binding.Apply(prev) })
	m := newTestInitManager(t, RuntimeInitConfig{OneShot: true})
	_, _, status, err := m.Apply(oneShotRequest())
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, status)
	original := binding.Current()
	for _, preserve := range []bool{false, true} {
		r := oneShotRequest()
		r.Generation++
		r.PreserveRuntimeState = preserve
		r.AccessTokenHash = "sha256:" + strings.Repeat("b", 64)
		_, _, status, err = m.Apply(r)
		require.Error(t, err)
		require.Equal(t, http.StatusConflict, status)
		require.Equal(t, original, binding.Current())
	}
}

func TestOneShotRequiresFullAuthenticatedInit(t *testing.T) {
	m := newTestInitManager(t, RuntimeInitConfig{OneShot: true})
	for _, r := range []*model.RuntimeInitRequest{validInitRequest(), oneShotRequest()} {
		if r.AccessTokenHash != "" {
			r.PreserveRuntimeState = true
		}
		_, _, status, err := m.Apply(r)
		require.Error(t, err)
		require.Equal(t, http.StatusBadRequest, status)
		require.False(t, m.Ready())
	}
}

func TestOneShotFailureRemainsLocked(t *testing.T) {
	prev := binding.Apply(nil)
	t.Cleanup(func() { binding.Apply(prev) })
	m := newTestInitManager(t, RuntimeInitConfig{
		OneShot:          true,
		EntrypointArgs:   []string{"/bin/false"},
		LaunchEntrypoint: func([]string) error { return errors.New("test startup failure") },
	})
	r := oneShotRequest()
	r.EntrypointPolicy = model.EntrypointPolicyRestart
	_, _, status, err := m.Apply(r)
	require.Error(t, err)
	require.Equal(t, http.StatusInternalServerError, status)
	require.False(t, m.Ready())
	_, _, status, err = m.Apply(oneShotRequest())
	require.Error(t, err)
	require.Equal(t, http.StatusConflict, status)
	require.False(t, m.Ready())
}

func TestOneShotConcurrentInit(t *testing.T) {
	prev := binding.Apply(nil)
	t.Cleanup(func() { binding.Apply(prev) })
	m := newTestInitManager(t, RuntimeInitConfig{OneShot: true})
	var wg sync.WaitGroup
	var successes atomic.Int32
	var conflicts atomic.Int32
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _, status, _ := m.Apply(oneShotRequest())
			if status == http.StatusOK {
				successes.Add(1)
			}
			if status == http.StatusConflict {
				conflicts.Add(1)
			}
		}()
	}
	wg.Wait()
	require.Equal(t, int32(1), successes.Load())
	require.Equal(t, int32(7), conflicts.Load())
}
