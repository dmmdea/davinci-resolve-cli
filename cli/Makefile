.PHONY: build test lint install clean

BIN_EXT := $(if $(filter windows,$(shell go env GOOS)),.exe,)

build:
	go build -o bin/davinci-resolve-pp-cli$(BIN_EXT) ./cmd/davinci-resolve-pp-cli

test:
	go test ./...

lint:
	golangci-lint run

install:
	go install ./cmd/davinci-resolve-pp-cli

clean:
	rm -rf bin/

build-mcp:
	go build -o bin/davinci-resolve-pp-mcp$(BIN_EXT) ./cmd/davinci-resolve-pp-mcp

install-mcp:
	go install ./cmd/davinci-resolve-pp-mcp

build-all: build build-mcp
