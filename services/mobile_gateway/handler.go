package main

import (
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/gorilla/websocket"

	pb "github.com/WatchMeJoustMyFlags/JoustMania/services/mobile_gateway/proto/mobile_controller"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool {
		return true // Allow all origins (phones on local network)
	},
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
}

// wsHandler manages WebSocket connections and delegates to PhoneSession.
type wsHandler struct {
	stub pb.MobileControllerServiceClient
}

func newWSHandler(stub pb.MobileControllerServiceClient) *wsHandler {
	return &wsHandler{stub: stub}
}

// Incoming JSON message types from the phone UI.
type wsMessage struct {
	Type    string  `json:"type"`
	Name    string  `json:"name,omitempty"`
	AX      float32 `json:"ax,omitempty"`
	AY      float32 `json:"ay,omitempty"`
	AZ      float32 `json:"az,omitempty"`
	GX      float32 `json:"gx,omitempty"`
	GY      float32 `json:"gy,omitempty"`
	GZ      float32 `json:"gz,omitempty"`
	Buttons uint32  `json:"buttons,omitempty"`
	Battery int32   `json:"battery,omitempty"`
}

// Outgoing JSON message types to the phone UI.
type wsJoinedResponse struct {
	Type   string `json:"type"`
	Serial string `json:"serial"`
}

type wsErrorResponse struct {
	Type    string `json:"type"`
	Message string `json:"message"`
}

type wsLedResponse struct {
	Type string `json:"type"`
	R    uint32 `json:"r"`
	G    uint32 `json:"g"`
	B    uint32 `json:"b"`
}

type wsRumbleResponse struct {
	Type      string `json:"type"`
	Intensity uint32 `json:"intensity"`
}

func (h *wsHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("WebSocket upgrade failed: %v", err)
		return
	}

	// Configure WebSocket connection
	_ = conn.SetReadDeadline(time.Time{}) // No read deadline (persistent connection)
	conn.SetPongHandler(func(string) error {
		return nil
	})

	session := newPhoneSession(conn, h.stub)

	mobileConnectionsActive.Add(r.Context(), 1)
	mobileConnectionsTotal.Add(r.Context(), 1)

	defer func() {
		session.close()
		mobileConnectionsActive.Add(r.Context(), -1)
		log.Printf("Phone %s disconnected", session.serial)
	}()

	// Start ping ticker for keepalive (equivalent to aiohttp heartbeat=30)
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if err := conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(5*time.Second)); err != nil {
					return
				}
			case <-session.done:
				return
			}
		}
	}()

	// Read loop
	for {
		_, msgBytes, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure) {
				log.Printf("WebSocket read error for %s: %v", session.serial, err)
			}
			return
		}

		var msg wsMessage
		if err := json.Unmarshal(msgBytes, &msg); err != nil {
			continue // Skip malformed JSON
		}

		switch msg.Type {
		case "join":
			name := msg.Name
			if name == "" {
				name = "Phone"
			}
			if session.register(r.Context(), name) {
				session.startStream(r.Context())
				resp := wsJoinedResponse{Type: "joined", Serial: session.serial}
				data, _ := json.Marshal(resp)
				if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
					return
				}
			} else {
				resp := wsErrorResponse{Type: "error", Message: "Registration failed"}
				data, _ := json.Marshal(resp)
				if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
					return
				}
			}

		case "motion":
			session.sendSensorData(r.Context(), &msg)

		case "button":
			session.sendButton(msg.Buttons)
		}
	}
}
