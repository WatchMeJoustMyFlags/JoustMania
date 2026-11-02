package flags

import (
	"context"
	"errors"
	"testing"
)

// UC: UC-047 BR-003
func TestCalibrationTestSession(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-047/BR-003 on and off are known", func(t *testing.T) {
		for _, want := range []bool{true, false} {
			f := New(stubEvaluator{booleans: map[string]bool{keyCalibrationTestSession: want}}, nil)
			on, known := f.CalibrationTestSession(ctx)
			if !known || on != want {
				t.Fatalf("CalibrationTestSession = (%v, %v), want (%v, true)", on, known, want)
			}
		}
	})
	t.Run("UC-047/BR-003 an unreadable mark is unknown, never off", func(t *testing.T) {
		f := New(stubEvaluator{errs: map[string]error{keyCalibrationTestSession: errors.New("flagd down")}}, nil)
		if on, known := f.CalibrationTestSession(ctx); known || on {
			t.Fatalf("CalibrationTestSession = (%v, %v), want (false, false)", on, known)
		}
	})
	t.Run("UC-047/BR-003 the key is calibration.test_session", func(t *testing.T) {
		if keyCalibrationTestSession != "calibration.test_session" {
			t.Fatalf("key = %q", keyCalibrationTestSession)
		}
	})
}
