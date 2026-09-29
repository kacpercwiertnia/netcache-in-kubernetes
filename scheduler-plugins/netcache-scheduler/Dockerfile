FROM golang:1.23-alpine@sha256:383395b794dffa5b53012a212365d40c8e37109a626ca30d6151c8348d380b5f AS build

WORKDIR /src
COPY go.mod go.mod
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/netcache-scheduler ./cmd/netcache-scheduler

FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
COPY --from=build /out/netcache-scheduler /usr/local/bin/netcache-scheduler
ENTRYPOINT ["/usr/local/bin/netcache-scheduler"]
