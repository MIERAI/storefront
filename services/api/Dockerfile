# context: the repository root (one module, one Dockerfile per service)
FROM golang:1.27.4-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY services/api/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/api .

FROM gcr.io/distroless/static-debian12:nonroot
ARG VERSION=dev
ENV APP_VERSION=${VERSION}
COPY --from=build /out/api /api
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/api"]
