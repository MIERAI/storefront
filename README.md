# storefront

> Part of the [GitOps Platform Blueprint](https://github.com/MIERAI/gitops-platform-blueprint)

A monorepo with three services, each released on its own (`release-<service>`: only changes under its directory) and
promoted on its own through three environments, dev → staging → prod:

| Service | | Manifests (gitops-manifests) |
|---|---|---|
| `services/web` | the page | kustomize `storefront/overlays/<env>` |
| `services/api` | products | kustomize `storefront/overlays/<env>` |
| `services/worker` | background jobs | Helm chart `storefront-worker/chart` + `values-<env>.yaml` | The third reference shape of the platform, next to
sample-app (one service, push) and api-factory (design PR → generated code).
