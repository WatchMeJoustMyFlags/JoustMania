package main

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/WatchMeJoustMyFlags/JoustMania/services/mobile_gateway/proto/mobile_controller"
)

// PhoneSession manages a single phone's WebSocket + gRPC lifecycle.
type PhoneSession struct {
	ws     *websocket.Conn
	stub   pb.MobileControllerServiceClient
	serial string
	stream pb.MobileControllerService_StreamPhoneDataClient
	seq    atomic.Uint64
	done   chan struct{}
}

func newPhoneSession(ws *websocket.Conn, stub pb.MobileControllerServiceClient) *PhoneSession {
	return &PhoneSession{
		ws:   ws,
		stub: stub,
		done: make(chan struct{}),
	}
}

// register calls RegisterPhone gRPC and stores the assigned serial.
func (s *PhoneSession) register(ctx context.Context, name string) bool {
	resp, err := s.stub.RegisterPhone(ctx, &pb.RegisterPhoneRequest{Name: name})
	if err != nil {
		log.Printf("RegisterPhone RPC failed: %v", err)
		return false
	}
	if !resp.GetSuccess() {
		log.Printf("Failed to register phone (name=%q)", name)
		return false
	}
	s.serial = resp.GetSerial()
	log.Printf("Registered phone %s (name=%q)", s.serial, name)
	return true
}

// unregister calls UnregisterPhone gRPC.
func (s *PhoneSession) unregister(ctx context.Context) {
	if s.serial == "" {
		return
	}
	_, err := s.stub.UnregisterPhone(ctx, &pb.UnregisterPhoneRequest{Serial: s.serial})
	if err != nil {
		log.Printf("UnregisterPhone RPC failed: %v", err)
		return
	}
	log.Printf("Unregistered phone %s", s.serial)
}

// startStream opens the bidirectional gRPC stream and starts the command relay goroutine.
func (s *PhoneSession) startStream(ctx context.Context) {
	stream, err := s.stub.StreamPhoneData(ctx)
	if err != nil {
		log.Printf("StreamPhoneData failed for %s: %v", s.serial, err)
		return
	}
	s.stream = stream

	// Relay commands from gRPC back to phone WebSocket
	go s.relayCommands()
}

// sendSensorData sends normalized sensor data to controller-manager via gRPC stream.
func (s *PhoneSession) sendSensorData(ctx context.Context, msg *wsMessage) {
	if s.stream == nil || s.serial == "" {
		return
	}

	seq := s.seq.Add(1)
	start := time.Now()

	update := &pb.PhoneDataUpdate{
		Serial: s.serial,
		Accel: &pb.Vector3{
			X: msg.AX,
			Y: msg.AY,
			Z: msg.AZ,
		},
		Gyro: &pb.Vector3{
			X: msg.GX,
			Y: msg.GY,
			Z: msg.GZ,
		},
		Buttons: msg.Buttons,
		Battery: msg.Battery,
		Seq:     seq,
	}

	if err := s.stream.Send(update); err != nil {
		log.Printf("gRPC stream send failed for %s: %v", s.serial, err)
		return
	}

	elapsed := time.Since(start).Seconds()
	mobileWSLatencySeconds.Record(ctx, elapsed)
}

// sendButton sends a button state update via the gRPC stream.
func (s *PhoneSession) sendButton(buttons uint32) {
	if s.stream == nil || s.serial == "" {
		return
	}

	seq := s.seq.Add(1)
	update := &pb.PhoneDataUpdate{
		Serial: s.serial,
		Accel: &pb.Vector3{
			X: 0,
			Y: 0,
			Z: 1,
		},
		Buttons: buttons,
		Battery: 100,
		Seq:     seq,
	}

	if err := s.stream.Send(update); err != nil {
		log.Printf("gRPC stream send (button) failed for %s: %v", s.serial, err)
	}
}

// relayCommands reads PhoneCommand messages from gRPC and relays them to the WebSocket.
func (s *PhoneSession) relayCommands() {
	if s.stream == nil {
		return
	}

	for {
		cmd, err := s.stream.Recv()
		if err != nil {
			if err == io.EOF {
				return
			}
			st, ok := status.FromError(err)
			if ok && st.Code() == codes.Canceled {
				return // Expected on disconnect
			}
			log.Printf("gRPC stream error for %s: %v", s.serial, err)
			return
		}

		var data []byte
		switch c := cmd.GetCommand().(type) {
		case *pb.PhoneCommand_Led:
			resp := wsLedResponse{
				Type: "led",
				R:    c.Led.GetR(),
				G:    c.Led.GetG(),
				B:    c.Led.GetB(),
			}
			data, _ = json.Marshal(resp)

		case *pb.PhoneCommand_Rumble:
			resp := wsRumbleResponse{
				Type:      "rumble",
				Intensity: c.Rumble.GetIntensity(),
			}
			data, _ = json.Marshal(resp)

		default:
			continue
		}

		if err := s.ws.WriteMessage(websocket.TextMessage, data); err != nil {
			return // WebSocket write failed, likely disconnected
		}
	}
}

// close cleans up the gRPC stream and unregisters the phone.
func (s *PhoneSession) close() {
	close(s.done)

	if s.stream != nil {
		_ = s.stream.CloseSend()
	}

	s.unregister(context.Background())
	_ = s.ws.Close()
}
