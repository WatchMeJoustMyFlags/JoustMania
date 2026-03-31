// Mobile Gateway Service — serves phone UI and bridges WebSocket to gRPC.
//
// Runs on port 8070. Connects to controller-manager MobileControllerService
// on port 50063 via gRPC.
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	pb "github.com/WatchMeJoustMyFlags/JoustMania/services/mobile_gateway/proto/mobile_controller"
)

func getEnv(key, defaultValue string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultValue
}

func main() {
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	controllerHost := getEnv("CONTROLLER_MANAGER_HOST", "controller-manager")
	controllerPort := getEnv("CONTROLLER_MANAGER_MOBILE_PORT", "50063")
	target := controllerHost + ":" + controllerPort

	// Initialize OTEL metrics
	shutdownMetrics := initMetrics()
	defer shutdownMetrics()

	// Create gRPC connection to controller-manager
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to create gRPC client to %s: %v", target, err)
	}
	defer conn.Close()

	stub := pb.NewMobileControllerServiceClient(conn)
	log.Printf("gRPC client targeting %s", target)

	// Determine static file directory
	// In Docker: /static/
	// In development: ./static/
	staticDir := "/static"
	if _, err := os.Stat(staticDir); os.IsNotExist(err) {
		staticDir = "static"
	}

	// HTTP mux
	mux := http.NewServeMux()

	// Health check
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("OK"))
	})

	// WebSocket endpoint
	wsHandler := newWSHandler(stub)
	mux.HandleFunc("/mobile/ws", wsHandler.ServeHTTP)

	// Static files for phone UI at /mobile/
	fileServer := http.FileServer(http.Dir(staticDir))
	mux.Handle("/mobile/", http.StripPrefix("/mobile/", fileServer))

	server := &http.Server{
		Addr:              ":8070",
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Graceful shutdown on SIGINT/SIGTERM
	done := make(chan os.Signal, 1)
	signal.Notify(done, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("Mobile Gateway listening on port 8070")
		log.Printf("Phone UI: http://0.0.0.0:8070/mobile/")
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("HTTP server error: %v", err)
		}
	}()

	<-done
	log.Println("Shutting down...")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := server.Shutdown(ctx); err != nil {
		log.Printf("HTTP server shutdown error: %v", err)
	}
}
