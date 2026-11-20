# context: the repository root (one module, one Dockerfile per service)
FROM golang:1.27.4-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY internal/ internal/
COPY services/web/ services/web/
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/web ./services/web

FROM gcr.io/distroless/static-debian12:nonroot
ARG VERSION=dev
ENV APP_VERSION=${VERSION}
COPY --from=build /out/web /web
EXPOSE 8080 9090
USER nonroot:nonroot
ENTRYPOINT ["/web"]
